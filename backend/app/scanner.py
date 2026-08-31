"""Filesystem scanning and batch embedding — extracted from indexer.py."""
from __future__ import annotations

import json
import logging
import os
import time
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

import numpy as np

from . import db, embeddings, metadata
from .models.scan import DiskFile, ScanReport, sha256_file, snapshot_meta_to_disk_file as _snapshot_meta_to_disk_file

log = logging.getLogger(__name__)

MTIME_TOLERANCE_SEC = 2.0
SCAN_DECODE_MAX_SIDE = 512


# ---------------------------------------------------------------------------
# Inventory helpers (free functions)
# ---------------------------------------------------------------------------

def _walk_store(settings, pause=None) -> dict[str, DiskFile]:
    found: dict[str, DiskFile] = {}
    root = Path(settings.store_path)
    exts = settings.extensions
    for dirpath, dirnames, filenames in os.walk(root):
        if pause:
            pause()
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        dp = Path(dirpath)
        for name in sorted(filenames):
            if pause:
                pause()
            if Path(name).suffix.lower() not in exts:
                continue
            ap = dp / name
            try:
                st = os.stat(ap)
            except OSError as exc:
                log.warning("stat failed for %s: %s", ap, exc)
                continue
            rel = ap.relative_to(root).as_posix()
            found[rel] = DiskFile(rel, ap, st.st_size, st.st_mtime)
    return found


def _is_snapshot_stale(settings, path: Path, payload: dict | None = None) -> bool:
    max_age_hours = getattr(settings, "snapshot_max_age_hours", 0)
    if not max_age_hours or max_age_hours <= 0:
        return False
    max_age_sec = max_age_hours * 3600
    try:
        age_file = time.time() - path.stat().st_mtime
        if age_file > max_age_sec:
            return True
    except OSError:
        return False
    return False


def _load_snapshot_inventory(settings, status: dict | None = None) -> dict[str, DiskFile] | None:
    if not settings.use_store_snapshot_for_initial_scan:
        return None
    path = settings.latest_store_snapshot_file
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not read store snapshot %s: %s", path.name, exc)
        return None

    if _is_snapshot_stale(settings, path, payload if isinstance(payload, dict) else None):
        try:
            age = round(time.time() - path.stat().st_mtime, 1)
        except OSError:
            age = -1
        log.warning(
            "store snapshot %s is stale (age=%.1fs > %dh); falling back to walk",
            path.name, age, settings.snapshot_max_age_hours,
        )
        return None

    entries = payload.get("files") if isinstance(payload, dict) and "files" in payload else payload
    if not isinstance(entries, dict):
        log.warning("ignoring invalid store snapshot payload in %s", path.name)
        return None

    disk: dict[str, DiskFile] = {}
    for rel_path, meta in entries.items():
        if Path(rel_path).suffix.lower() not in settings.extensions:
            continue
        disk_file = _snapshot_meta_to_disk_file(settings.store_path, rel_path, meta)
        if disk_file is None:
            log.warning("ignoring invalid store snapshot entry for %s", rel_path)
            continue
        disk[disk_file.rel_path] = disk_file

    if not disk:
        log.warning("store snapshot %s contained no usable image entries", path.name)
        return None
    log.info("using store snapshot %s for inventory (%d files)", path.name, len(disk))
    if status is not None:
        status["inventory_source"] = "snapshot"
    return disk


def _sync_network_root(settings) -> None:
    """Update original_path prefixes when network_root has changed."""
    s = settings
    new_root = s.network_root or ""
    old_root = db.kv_get(s.db_path, "network_root") or ""
    if new_root == old_root:
        return
    if old_root and new_root:
        n = db.update_original_paths(s.db_path, old_root, new_root)
        if n:
            log.info("network_root changed %r -> %r; updated %d original_path rows",
                     old_root, new_root, n)
    db.kv_set(s.db_path, "network_root", new_root)


def _disk_files_for_rel_paths(settings, rel_paths: list[str], report: ScanReport) -> list[DiskFile]:
    pending: list[DiskFile] = []
    for rel_path in rel_paths:
        abs_path = settings.store_path / rel_path
        try:
            stat = abs_path.stat()
        except OSError as exc:
            report.failed += 1
            report.processed += 1
            report.errors.append(f"{rel_path}: {exc}")
            log.warning("resume stat failed: %s (%s)", rel_path, exc)
            continue
        pending.append(
            DiskFile(
                rel_path=rel_path,
                abs_path=abs_path,
                size=stat.st_size,
                mtime=stat.st_mtime,
            )
        )
    return pending


def _disk_files_from_snapshot(settings, rel_paths: list[str], report: ScanReport) -> list[DiskFile] | None:
    snapshot = _load_snapshot_inventory(settings)
    if snapshot is None:
        return None
    pending: list[DiskFile] = []
    for rel_path in rel_paths:
        disk_file = snapshot.get(rel_path.replace("\\", "/"))
        if disk_file is None:
            return None
        pending.append(disk_file)
    return pending


def _disk_file_from_live_stat(settings, rel_path: str, report: ScanReport) -> DiskFile | None:
    # Path containment check mirrors api/routes.py:_store_file to block
    # crafted delta payloads (e.g. "../../etc/passwd") from escaping the store.
    normalized = rel_path.replace("\\", "/")
    if normalized.startswith("/") or ".." in normalized.split("/"):
        log.warning("blocked delta path traversal attempt: rel_path=%r", rel_path)
        report.failed += 1
        report.processed += 1
        report.errors.append(f"{rel_path}: blocked path traversal")
        return None
    root = settings.store_path.resolve()
    abs_path = (settings.store_path / normalized).resolve()
    if not abs_path.is_relative_to(root):
        log.warning("blocked delta path escape: rel_path=%r resolved=%r", rel_path, str(abs_path))
        report.failed += 1
        report.processed += 1
        report.errors.append(f"{rel_path}: blocked path escape")
        return None
    try:
        stat = abs_path.stat()
    except OSError as exc:
        report.failed += 1
        report.processed += 1
        report.errors.append(f"{rel_path}: {exc}")
        log.warning("delta stat failed: %s (%s)", rel_path, exc)
        return None
    return DiskFile(
        rel_path=normalized,
        abs_path=abs_path,
        size=stat.st_size,
        mtime=stat.st_mtime,
    )


def _disk_file_for_delta_path(settings, rel_path: str, snapshot: dict[str, DiskFile] | None, report: ScanReport) -> DiskFile | None:
    normalized = rel_path.replace("\\", "/")
    if snapshot is not None:
        disk_file = snapshot.get(normalized)
        if disk_file is not None:
            return disk_file
        log.warning("delta path missing from store snapshot, falling back to live stat: %s", normalized)
    return _disk_file_from_live_stat(settings, normalized, report)


def _decode_and_hash(f: DiskFile):
    img = embeddings.decode_image(f.abs_path)
    dims = img.size  # original (width, height) for the DB row
    # Downscale in the worker: CLIP consumes 224 px inputs, so shipping
    # 100+ Mpixel frames (hundreds of MB RGB each) through the prefetch
    # window and the per-chunk accumulation list OOM-kills the container.
    img.thumbnail((SCAN_DECODE_MAX_SIDE, SCAN_DECODE_MAX_SIDE))
    return f, img, sha256_file(f.abs_path), dims


# ---------------------------------------------------------------------------
# Scanner class — owns batch/decode logic and inventory delegation
# ---------------------------------------------------------------------------

class Scanner:
    """Inventory + batch processing extracted from Indexer."""

    def __init__(self, settings, faiss_store, checkpoint_manager, status: dict, pause_gate, decode_pool: ThreadPoolExecutor, xmp_pool: ThreadPoolExecutor):
        self.settings = settings
        self.faiss_store = faiss_store
        self.checkpoint_manager = checkpoint_manager
        self.status = status
        self.pause_gate = pause_gate
        self._decode_pool = decode_pool
        self._xmp_pool = xmp_pool

    # inventory delegation
    def walk_store(self, pause=None):
        return _walk_store(self.settings, pause)

    def is_snapshot_stale(self, path: Path, payload: dict | None = None) -> bool:
        return _is_snapshot_stale(self.settings, path, payload)

    def load_snapshot_inventory(self) -> dict[str, DiskFile] | None:
        return _load_snapshot_inventory(self.settings, self.status)

    def sync_network_root(self) -> None:
        return _sync_network_root(self.settings)

    def disk_files_for_rel_paths(self, rel_paths: list[str], report: ScanReport) -> list[DiskFile]:
        return _disk_files_for_rel_paths(self.settings, rel_paths, report)

    def disk_files_from_snapshot(self, rel_paths: list[str], report: ScanReport) -> list[DiskFile] | None:
        snapshot = self.load_snapshot_inventory()
        if snapshot is None:
            return None
        pending: list[DiskFile] = []
        for rel_path in rel_paths:
            disk_file = snapshot.get(rel_path.replace("\\", "/"))
            if disk_file is None:
                return None
            pending.append(disk_file)
        return pending

    def disk_file_from_live_stat(self, rel_path: str, report: ScanReport) -> DiskFile | None:
        return _disk_file_from_live_stat(self.settings, rel_path, report)

    def disk_file_for_delta_path(self, rel_path: str, snapshot: dict[str, DiskFile] | None, report: ScanReport) -> DiskFile | None:
        return _disk_file_for_delta_path(self.settings, rel_path, snapshot, report)

    def decode_and_hash(self, f: DiskFile):
        return _decode_and_hash(f)

    # The larger batch helpers are kept here so Indexer can delegate but still
    # allow monkeypatching on Indexer facade wrappers. Implement thin wrappers
    # that match Indexer's former signatures and rely on injected callbacks.


__all__ = [
    "MTIME_TOLERANCE_SEC",
    "SCAN_DECODE_MAX_SIDE",
    "_walk_store",
    "_is_snapshot_stale",
    "_load_snapshot_inventory",
    "_sync_network_root",
    "_disk_files_for_rel_paths",
    "_disk_files_from_snapshot",
    "_disk_file_from_live_stat",
    "_disk_file_for_delta_path",
    "_decode_and_hash",
    "Scanner",
]
