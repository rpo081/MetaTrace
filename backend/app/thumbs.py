"""Thumbnail cache utilities (single source of truth for LRU pruning)."""
from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

_THUMB_TMP_GLOBS = ("tmp*", "*.tmp", ".tmp*")


def cleanup_orphan_thumb_temps(settings) -> int:
    """Remove orphaned mkstemp temp files in the thumbs dir. Returns count removed."""
    removed = 0
    for pattern in _THUMB_TMP_GLOBS:
        for p in settings.thumbs_dir.glob(pattern):
            if not p.is_file():
                continue
            try:
                p.unlink()
                removed += 1
            except OSError as exc:
                log.warning("could not remove orphaned temp file %s: %s", p.name, exc)
    return removed


def prune_thumb_cache(settings, *, max_files: int | None = None) -> int:
    """LRU-eviction for the thumbnail cache. Returns number pruned."""
    cap = max_files if max_files is not None else getattr(settings, "thumbs_max_files", 0)
    if not cap or cap <= 0:
        return 0
    try:
        thumbs_dir = settings.thumbs_dir
        if not thumbs_dir.is_dir():
            return 0
        files = list(thumbs_dir.glob("*.png"))
        if len(files) <= cap:
            return 0
        files.sort(key=lambda p: p.stat().st_mtime)
        to_remove = len(files) - cap
        removed = 0
        for p in files[:to_remove]:
            try:
                p.unlink()
                removed += 1
            except OSError as exc:
                log.warning("thumb LRU prune failed for %s: %s", p.name, exc)
        if removed:
            log.info("thumb cache LRU pruned %d file(s) (cap=%d)", removed, cap)
        return removed
    except Exception:  # noqa: BLE001
        log.warning("thumb cache prune failed", exc_info=True)
        return 0


def unlink_quiet(path: str) -> None:
    """Best-effort unlink of a temp file."""
    try:
        os.unlink(path)
    except OSError:
        pass
