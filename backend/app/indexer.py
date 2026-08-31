"""Filesystem scanner and incremental embedding indexer (SQLite + FAISS).

Facade over :mod:`faiss_store`, :mod:`checkpoint`, and :mod:`scanner`.
Public API is stable: ``Indexer(settings)`` with methods
``load_or_create``, ``incremental``, ``request_pause``, ``resume``,
``has_resume_checkpoint``, ``resume_from_checkpoint``, ``count``, ``lock``,
``status``, ``index``.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import Counter, deque
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

import numpy as np

from . import db, embeddings, metadata
from .checkpoint import (
    SCAN_CHECKPOINT_VERSION,
    CheckpointManager,
    _clear_resume_checkpoint as _cp_clear,
    _read_resume_checkpoint as _cp_read,
    _report_from_dict as _cp_report,
    _save_pending_checkpoint as _cp_save_pending,
    _save_planning_checkpoint as _cp_save_planning,
    _write_resume_checkpoint as _cp_write,
)
from .config import Settings
from .faiss_store import FaissStore, _quarantine_corrupt_index
from .models.scan import DiskFile, ScanReport, sha256_file, snapshot_meta_to_disk_file as _snapshot_meta_to_disk_file
from .scanner import (
    MTIME_TOLERANCE_SEC,
    SCAN_DECODE_MAX_SIDE,
    Scanner,
    _disk_file_for_delta_path as _sc_disk_file_for_delta_path,
    _disk_file_from_live_stat as _sc_disk_file_from_live_stat,
    _disk_files_for_rel_paths as _sc_disk_files_for_rel_paths,
    _disk_files_from_snapshot as _sc_disk_files_from_snapshot,
    _is_snapshot_stale as _sc_is_snapshot_stale,
    _load_snapshot_inventory as _sc_load_snapshot_inventory,
    _sync_network_root as _sc_sync_network_root,
    _walk_store as _sc_walk_store,
)

log = logging.getLogger(__name__)

PUBLISH_INTERVAL_SEC = 30.0

# Re-export for callers/tests that import from indexer
__all__ = ["Indexer", "ScanReport", "DiskFile", "sha256_file"]


class Indexer:
    """Owns the FAISS index; all mutations happen under an internal RLock.

    Concurrency model: scans build/mutate a *working copy* of the index and
    swap it into ``self.index`` atomically under ``_lock`` at the end. Readers
    may snapshot the reference under ``lock`` and use it without holding the
    lock — a swapped-out index object is never mutated again.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self._faiss_store = FaissStore(settings)
        self._checkpoint = CheckpointManager(settings)
        self._scan_lock = threading.Lock()
        self._pause_gate = threading.Event()
        self._pause_gate.set()
        self.status: dict = {"state": "idle", "last_report": None, "inventory_source": None}
        # Worker threads spawn lazily on first submit; scans are serialized by
        # _scan_lock, so the pools never run concurrently.
        self._decode_pool = ThreadPoolExecutor(
            max_workers=max(1, settings.decode_workers),
            thread_name_prefix="metatrace-decode",
        )
        self._xmp_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="metatrace-xmp")
        self._scanner = Scanner(
            settings,
            self._faiss_store,
            self._checkpoint,
            self.status,
            self._pause_gate,
            self._decode_pool,
            self._xmp_pool,
        )

    # ------------------------------------------------------------------ api
    @property
    def count(self) -> int:
        return self._faiss_store.count

    @property
    def lock(self) -> threading.RLock:
        return self._faiss_store.lock

    @property
    def _lock(self) -> threading.RLock:
        return self._faiss_store._lock

    @_lock.setter
    def _lock(self, value: threading.RLock) -> None:
        self._faiss_store._lock = value

    @property
    def index(self):
        return self._faiss_store.index

    @index.setter
    def index(self, value) -> None:
        self._faiss_store.index = value

    @property
    def _resume_checkpoint(self) -> dict | None:
        return self._checkpoint._resume_checkpoint

    @_resume_checkpoint.setter
    def _resume_checkpoint(self, value: dict | None) -> None:
        self._checkpoint._resume_checkpoint = value

    @property
    def _last_publish_monotonic(self) -> float:
        return self._faiss_store._last_publish_monotonic

    @_last_publish_monotonic.setter
    def _last_publish_monotonic(self, value: float) -> None:
        self._faiss_store._last_publish_monotonic = value

    @property
    def _resume_checkpoint_file(self) -> Path:
        return self._checkpoint.checkpoint_file

    # delegation helpers for monkeypatch compatibility
    def _walk_store(self, pause=None):
        return self._scanner.walk_store(pause)

    def _is_snapshot_stale(self, path: Path, payload: dict | None = None) -> bool:
        return self._scanner.is_snapshot_stale(path, payload)

    def _load_snapshot_inventory(self):
        return self._scanner.load_snapshot_inventory()

    def _sync_network_root(self) -> None:
        return self._scanner.sync_network_root()

    def _disk_files_for_rel_paths(self, rel_paths: list[str], report: ScanReport):
        return self._scanner.disk_files_for_rel_paths(rel_paths, report)

    def _disk_files_from_snapshot(self, rel_paths: list[str], report: ScanReport):
        return self._scanner.disk_files_from_snapshot(rel_paths, report)

    def _disk_file_from_live_stat(self, rel_path: str, report: ScanReport):
        return self._scanner.disk_file_from_live_stat(rel_path, report)

    def _disk_file_for_delta_path(self, rel_path: str, snapshot, report: ScanReport):
        return self._scanner.disk_file_for_delta_path(rel_path, snapshot, report)

    def _decode_and_hash(self, f: DiskFile):
        return self._scanner.decode_and_hash(f)

    def _clone_published_index(self):
        return self._faiss_store.clone_published_index()

    def _publish_index(self, index) -> None:
        return self._faiss_store.publish_index(index)

    def _read_resume_checkpoint(self) -> dict | None:
        return _cp_read(self.settings)

    def _write_resume_checkpoint(self, payload: dict) -> None:
        _cp_write(self.settings, payload)
        self._checkpoint._resume_checkpoint = payload

    def _clear_resume_checkpoint(self) -> None:
        self._checkpoint.clear()

    def _save_planning_checkpoint(self, mode: str, force_rebuild: bool, report: ScanReport, delta_info: dict | None = None) -> None:
        _cp_save_planning(self.settings, mode, force_rebuild, report, delta_info)
        self._checkpoint._resume_checkpoint = _cp_read(self.settings)

    def _save_pending_checkpoint(self, mode: str, force_rebuild: bool, report: ScanReport, remaining_rel_paths: list[str], remaining_added_rel_paths: set[str]) -> None:
        _cp_save_pending(self.settings, mode, force_rebuild, report, remaining_rel_paths, remaining_added_rel_paths)
        self._checkpoint._resume_checkpoint = _cp_read(self.settings)

    def _load_resume_checkpoint(self) -> None:
        cp = self._checkpoint.load(self.status, self._pause_gate)
        # load() already clears stale planning checkpoints
        return None

    def _report_from_dict(self, data: dict) -> ScanReport:
        return _cp_report(data)

    def load_or_create(self) -> None:
        """Load the persisted index, reconciling it against the database."""
        import faiss

        s = self.settings
        db.init_db(s.db_path)
        idx_file = s.index_file

        if idx_file.exists():
            try:
                log.info("loading index from %s", idx_file)
                self.index = faiss.read_index(str(idx_file))
            except Exception as exc:  # noqa: BLE001 - corrupt file must not boot-loop
                quarantine = _quarantine_corrupt_index(idx_file)
                log.error(
                    "FAISS index unreadable (%s); quarantined as %s. "
                    "A full rebuild will run on the next scan.",
                    exc, quarantine.name,
                )
                self._force_full_rebuild("corrupt index file")
                return
            self._reconcile_counts()
        else:
            n_db = db.count(s.db_path)
            if n_db:
                log.warning(
                    "FAISS index file missing but DB still has %d rows; "
                    "forcing full rebuild on next scan",
                    n_db,
                )
                self._force_full_rebuild("missing index file")
        self._load_resume_checkpoint()

    def incremental(
        self,
        trigger: str = "manual",
        force_rebuild: bool = False,
        progress=None,
        delta_info: dict | None = None,
    ) -> ScanReport:
        report = ScanReport(trigger=trigger, started_at=time.time())
        with self._scan_lock:
            self._resume_checkpoint = None
            self._pause_gate.set()
            self._set_state("scanning")
            self.status["inventory_source"] = None
            self.status["last_report"] = report.as_dict()

            def publish(current: ScanReport) -> None:
                self.status["last_report"] = current.as_dict()
                if progress:
                    progress(current.as_dict())

            try:
                mode = "delta" if delta_info and not force_rebuild else "full"
                self._save_planning_checkpoint(mode, force_rebuild, report, delta_info)
                if delta_info and not force_rebuild:
                    self._process_delta(report, delta_info, publish)
                else:
                    self._scan(report, force_rebuild, publish)
            except Exception:
                self._set_state("error")
                raise
            finally:
                report.duration_sec = round(time.time() - report.started_at, 2)
                self.status["last_report"] = report.as_dict()
                if self.status["state"] == "paused":
                    self._resume_checkpoint = self._read_resume_checkpoint()
                elif self.status["state"] != "error":
                    self._clear_resume_checkpoint()
                    self._set_state("idle")
        return report

    def request_pause(self) -> bool:
        if self.status["state"] != "scanning":
            return False
        self._pause_gate.clear()
        return True

    def resume(self) -> bool:
        if self.status["state"] != "paused" or not self._scan_lock.locked():
            return False
        self._pause_gate.set()
        return True

    def has_resume_checkpoint(self) -> bool:
        return self._resume_checkpoint is not None or self._resume_checkpoint_file.exists()

    def resume_from_checkpoint(self) -> ScanReport:
        checkpoint = self._resume_checkpoint or self._read_resume_checkpoint()
        if checkpoint is None:
            raise RuntimeError("no persisted scan checkpoint available")
        if checkpoint.get("phase") != "pending":
            self._clear_resume_checkpoint()
            return self.incremental(
                trigger=checkpoint.get("trigger", "resume"),
                force_rebuild=bool(checkpoint.get("force_rebuild", False)),
                delta_info=checkpoint.get("delta_info"),
            )

        report = self._report_from_dict(checkpoint.get("report", {}))
        with self._scan_lock:
            self._pause_gate.set()
            self._set_state("scanning")
            self.status["last_report"] = report.as_dict()

            def publish(current: ScanReport) -> None:
                self.status["last_report"] = current.as_dict()

            working = self._clone_published_index()
            remaining_rel_paths = checkpoint.get("remaining_rel_paths", [])
            pending = self._disk_files_from_snapshot(remaining_rel_paths, report)
            if pending is None:
                self._set_inventory_source("walk")
                pending = self._disk_files_for_rel_paths(remaining_rel_paths, report)
            pending_added = set(checkpoint.get("remaining_added_rel_paths", []))
            known = db.list_entries(self.settings.db_path)
            try:
                working = self._run_pending_batches(
                    report=report,
                    working=working,
                    pending=pending,
                    added_rel_paths=pending_added,
                    known=known,
                    mode=str(checkpoint.get("mode", "full")),
                    force_rebuild=bool(checkpoint.get("force_rebuild", False)),
                    progress=publish,
                )
                self._publish_index(working)
                db.kv_set(
                    self.settings.db_path,
                    "last_scan",
                    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                )
                self._clear_resume_checkpoint()
                log.info(
                    "resumed scan done (%s): seen=%d added=%d updated=%d removed=%d failed=%d in %.1fs",
                    report.trigger,
                    report.seen,
                    report.added,
                    report.updated,
                    report.removed,
                    report.failed,
                    report.duration_sec or 0.0,
                )
            except Exception:
                self._set_state("error")
                raise
            finally:
                report.duration_sec = round(time.time() - report.started_at, 2)
                self.status["last_report"] = report.as_dict()
                if self.status["state"] != "error":
                    self._set_state("idle")
        return report

    # ------------------------------------------------------------ internals
    def _set_state(self, state: str) -> None:
        self.status["state"] = state

    def _set_inventory_source(self, source: str | None) -> None:
        self.status["inventory_source"] = source

    def _wait_if_paused(self, report: ScanReport, progress=None, snapshot=None) -> None:
        if self._pause_gate.is_set():
            return
        if snapshot:
            snapshot()
        pause_start = time.time()
        if self.status["state"] != "paused":
            self._set_state("paused")
            self.status["last_report"] = report.as_dict()
            if progress:
                progress(report)
        self._pause_gate.wait()
        paused_delta = max(0.0, time.time() - pause_start)
        report.paused_duration_sec += paused_delta
        if self.status["state"] == "paused":
            self._clear_resume_checkpoint()
            self._set_state("scanning")
            self.status["last_report"] = report.as_dict()
            if progress:
                progress(report)

    def _model_key(self) -> str:
        return f"{self.settings.model_name}:{self.settings.model_pretrained}"

    def _snapshot_pause_state(
        self,
        report: ScanReport,
        working,
        mode: str,
        force_rebuild: bool,
        remaining: list[DiskFile],
        added_rel_paths: set[str],
    ) -> None:
        import faiss

        self._publish_index(faiss.clone_index(working) if working is not None else None)
        self._save_pending_checkpoint(
            mode,
            force_rebuild,
            report,
            [f.rel_path for f in remaining],
            added_rel_paths,
        )

    def _publish_progress(self, index) -> None:
        import faiss
        import time

        now = time.monotonic()
        if now - self._last_publish_monotonic < PUBLISH_INTERVAL_SEC:
            return
        self._last_publish_monotonic = now
        self._publish_index(faiss.clone_index(index))

    def _force_full_rebuild(self, reason: str) -> None:
        log.warning("forcing full index rebuild (%s)", reason)
        db.reset(self.settings.db_path)
        with self._lock:
            self.index = None

    def _reconcile_counts(self) -> None:
        import faiss

        s = self.settings
        assert self.index is not None
        entries = db.list_entries(s.db_path)
        db_ids = {e.id for e in entries.values()}
        id_array = faiss.vector_to_array(self.index.id_map)
        index_ids = {int(i) for i in id_array}
        occurrences = Counter(int(i) for i in id_array)
        dups = sorted(i for i, n in occurrences.items() if n > 1)
        if dups:
            log.warning(
                "index contains %d duplicated vector id(s) (%d extra vector(s)); "
                "removing them for a clean re-embed",
                len(dups), int(self.index.ntotal) - len(occurrences),
            )
            self.index.remove_ids(np.array(dups, dtype="int64"))
            db.remove_ids(s.db_path, dups)
            db_ids = {e.id for e in db.list_entries(s.db_path).values()}
            index_ids -= set(dups)
        orphans = sorted(index_ids - db_ids)
        holes = sorted(db_ids - index_ids)
        if not orphans and not holes:
            return
        log.warning(
            "index/DB divergence: %d orphan vector(s), %d row(s) without vector; "
            "repairing surgically",
            len(orphans), len(holes),
        )
        if orphans:
            self.index.remove_ids(np.array(orphans, dtype="int64"))
        if holes:
            db.remove_ids(s.db_path, holes)
        self._publish_index(self.index)
        log.info(
            "index/DB repair done: faiss.ntotal=%d, db rows=%d",
            int(self.index.ntotal), db.count(s.db_path),
        )

    def _run_pending_batches(
        self,
        *,
        report: ScanReport,
        working,
        pending: list[DiskFile],
        added_rel_paths: set[str],
        known: dict[str, object],
        mode: str,
        force_rebuild: bool,
        progress,
    ):
        batch = max(1, self.settings.batch_size)
        for i in range(0, len(pending), batch):
            remaining = pending[i:]
            remainder = pending[i + batch :]
            self._wait_if_paused(
                report,
                progress,
                snapshot=lambda remaining=remaining, working=working: self._snapshot_pause_state(
                    report,
                    working,
                    mode,
                    force_rebuild,
                    remaining,
                    added_rel_paths,
                ),
            )
            chunk = pending[i : i + batch]
            working = self._process_chunk(
                chunk,
                added_rel_paths,
                known,
                report,
                working,
                mode,
                force_rebuild,
                remainder,
                progress,
            )
            if progress:
                progress(report)
        return working

    def _scan(self, report: ScanReport, force_rebuild: bool, progress) -> None:
        s = self.settings
        db.init_db(s.db_path, configure_journal=False)
        known = db.list_entries(s.db_path)
        disk = self._load_snapshot_inventory()
        if disk is None:
            self._set_inventory_source("walk")
            disk = self._walk_store(
                lambda: self._wait_if_paused(
                    report,
                    progress,
                    snapshot=lambda: self._save_planning_checkpoint("full", force_rebuild, report),
                )
            )
        report.seen = len(disk)
        stored_model = db.kv_get(s.db_path, "model")
        current_model = self._model_key()
        if stored_model and stored_model != current_model:
            log.warning("embedding model changed %s -> %s; rebuilding index",
                        stored_model, current_model)
            force_rebuild = True
        if force_rebuild:
            db.reset(s.db_path)
            known = {}
            working = None
        else:
            working = self._clone_published_index()
        db.kv_set(s.db_path, "model", current_model)
        to_add = [f for r, f in disk.items() if r not in known]
        to_update = [
            f for r, f in disk.items()
            if r in known and (
                known[r].size != f.size
                or abs(known[r].mtime - f.mtime) > MTIME_TOLERANCE_SEC
            )
        ]
        removed_ids = sorted(known[r].id for r in known if r not in disk)
        report.unchanged = len(disk) - len(to_add) - len(to_update)
        report.processed = report.unchanged
        if progress:
            progress(report)
        if removed_ids:
            if working is not None:
                working.remove_ids(np.array(removed_ids, dtype="int64"))
            db.remove_ids(s.db_path, removed_ids)
            report.removed = len(removed_ids)
        pending = to_add + to_update
        added_rel_paths = {f.rel_path for f in to_add}
        working = self._run_pending_batches(
            report=report,
            working=working,
            pending=pending,
            added_rel_paths=added_rel_paths,
            known=known,
            mode="full",
            force_rebuild=force_rebuild,
            progress=progress,
        )
        self._publish_index(working)
        self._sync_network_root()
        db.kv_set(s.db_path, "last_scan", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        log.info(
            "scan done (%s): seen=%d added=%d updated=%d removed=%d failed=%d in %.1fs",
            report.trigger, report.seen, report.added, report.updated,
            report.removed, report.failed, report.duration_sec or 0.0,
        )

    def _process_delta(self, report: ScanReport, delta_info: dict, progress) -> None:
        s = self.settings
        db.init_db(s.db_path, configure_journal=False)
        known = db.list_entries(s.db_path)
        changes = delta_info.get("changes", {})
        deleted_paths = set(changes.get("deleted", []))
        created_paths = set(changes.get("created", []))
        modified_paths = set(changes.get("modified", []))
        snapshot = self._load_snapshot_inventory()
        report.seen = len(deleted_paths) + len(created_paths) + len(modified_paths)
        working = self._clone_published_index()
        removed_ids = []
        for rel_path in deleted_paths:
            self._wait_if_paused(
                report,
                progress,
                snapshot=lambda: self._save_planning_checkpoint("delta", False, report, delta_info),
            )
            log.info("checking deleted rel_path: %r (in known: %s)", rel_path, rel_path in known)
            if rel_path in known:
                removed_ids.append(known[rel_path].id)
                log.debug("removing from index: %s (id=%d)", rel_path, known[rel_path].id)
            else:
                log.warning("deleted path not found in DB: %r", rel_path)
                if known:
                    log.warning("known paths sample (first 3): %s",
                               list(known.keys())[:3])
        log.info("delta: deleted_paths=%d, removed_ids=%d", len(deleted_paths), len(removed_ids))
        if removed_ids:
            if working is not None:
                working.remove_ids(np.array(removed_ids, dtype="int64"))
            db.remove_ids(s.db_path, removed_ids)
            report.removed = len(removed_ids)
            report.processed += len(removed_ids)
            if progress:
                progress(report)
        to_process = []
        added_set = set()
        for rel_path in created_paths:
            self._wait_if_paused(
                report,
                progress,
                snapshot=lambda: self._save_planning_checkpoint("delta", False, report, delta_info),
            )
            disk_file = self._disk_file_for_delta_path(rel_path, snapshot, report)
            if disk_file is not None:
                to_process.append(disk_file)
                added_set.add(disk_file.rel_path)
        for rel_path in modified_paths:
            self._wait_if_paused(
                report,
                progress,
                snapshot=lambda: self._save_planning_checkpoint("delta", False, report, delta_info),
            )
            if rel_path in known:
                disk_file = self._disk_file_for_delta_path(rel_path, snapshot, report)
                if disk_file is not None:
                    to_process.append(disk_file)
        working = self._run_pending_batches(
            report=report,
            working=working,
            pending=to_process,
            added_rel_paths=added_set,
            known=known,
            mode="delta",
            force_rebuild=False,
            progress=progress,
        )
        self._publish_index(working)
        self._sync_network_root()
        log.info("published index to self.index, new ntotal=%d", (working.ntotal if working else 0))
        db.kv_set(s.db_path, "last_scan", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        delta_file = s.data_path / "rescan_delta_latest.json"
        try:
            delta_file.unlink(missing_ok=True)
            log.info("cleaned up delta file after processing")
        except Exception as exc:  # noqa: BLE001
            log.warning("could not remove delta file: %s", exc)
        log.info(
            "delta scan done (%s): deleted=%d created=%d modified=%d added=%d updated=%d failed=%d in %.1fs",
            report.trigger, len(deleted_paths), len(created_paths), len(modified_paths),
            report.added, report.updated, report.failed, report.duration_sec or 0.0,
        )

    def _decode_and_hash(self, f: DiskFile):
        return self._scanner.decode_and_hash(f)

    def _process_chunk(
        self,
        chunk: list[DiskFile],
        added_set: set[str],
        known: dict[str, object],
        report: ScanReport,
        index,
        mode: str,
        force_rebuild: bool,
        remainder: list[DiskFile],
        progress=None,
    ):
        import faiss

        s = self.settings
        images: list = []
        ok_files: list[DiskFile] = []
        shas: dict[str, str] = {}
        dims: dict[str, tuple[int, int]] = {}

        window = max(1, s.decode_prefetch)
        inflight: deque[tuple[int, Future]] = deque()
        next_offset = 0

        def submit_next() -> None:
            nonlocal next_offset
            if next_offset < len(chunk):
                fut = self._decode_pool.submit(self._scanner.decode_and_hash, chunk[next_offset])
                inflight.append((next_offset, fut))
                next_offset += 1

        for _ in range(min(window, len(chunk))):
            submit_next()

        while inflight:
            offset, fut = inflight.popleft()
            f = chunk[offset]
            remaining = chunk[offset:] + remainder
            self._wait_if_paused(
                report,
                progress,
                snapshot=lambda remaining=remaining, index=index: self._snapshot_pause_state(
                    report,
                    index,
                    mode,
                    force_rebuild,
                    remaining,
                    added_set,
                ),
            )
            try:
                done_file, img, sha, dims_xy = fut.result()
                images.append(img)
                ok_files.append(done_file)
                shas[done_file.rel_path] = sha
                dims[done_file.rel_path] = dims_xy
            except Exception as exc:  # noqa: BLE001
                report.failed += 1
                report.errors.append(f"{f.rel_path}: {exc}")
                log.warning("decode failed: %s (%s)", f.rel_path, exc)
            finally:
                report.processed += 1
                if progress:
                    progress(report)
            submit_next()
        if not images:
            return index

        xmp_future = self._xmp_pool.submit(
            metadata.extract_xmp, [f.abs_path for f in ok_files]
        )

        try:
            vectors = embeddings.embed_images(images, s)
        except Exception as exc:  # noqa: BLE001
            report.failed += len(ok_files)
            report.errors.append(f"embed batch failed: {exc}")
            log.exception("embed batch failed")
            return index

        xmp_map = xmp_future.result()

        update_ids = [
            known[f.rel_path].id
            for f in ok_files
            if f.rel_path in known
        ]
        if update_ids and index is not None:
            index.remove_ids(np.array(update_ids, dtype="int64"))

        if index is None:
            dim = int(vectors.shape[1])
            log.info("creating flat IP index (dim=%d)", dim)
            index = faiss.IndexIDMap2(faiss.IndexFlatIP(dim))

        rows = []
        n_added = n_updated = 0
        for f, _vec in zip(ok_files, vectors):
            width, height = dims[f.rel_path]
            rows.append(
                {
                    "rel_path": f.rel_path,
                    "original_path": s.original_path_for(f.rel_path),
                    "size": f.size,
                    "mtime": f.mtime,
                    "sha256": shas[f.rel_path],
                    "width": width,
                    "height": height,
                    "xmp": xmp_map.get(str(f.abs_path), {}),
                }
            )
            if f.rel_path in added_set:
                n_added += 1
            else:
                n_updated += 1
        id_by_rel = db.upsert_images_bulk(s.db_path, rows)
        ids = [id_by_rel[r["rel_path"]] for r in rows]
        index.add_with_ids(
            vectors.astype("float32"), np.array(ids, dtype="int64")
        )
        report.added += n_added
        report.updated += n_updated
        self._publish_progress(index)
        return index
