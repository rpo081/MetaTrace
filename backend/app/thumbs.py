"""Thumbnail cache utilities (single source of truth for LRU pruning)."""
from __future__ import annotations

import logging
import os
import tempfile
import threading
import time
from collections import deque
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from . import db, embeddings

log = logging.getLogger(__name__)

_THUMB_TMP_GLOBS = ("tmp*", "*.tmp", ".tmp*")
_generation_locks = tuple(threading.Lock() for _ in range(256))


def _generation_lock(path: Path) -> threading.Lock:
    return _generation_locks[hash(path) % len(_generation_locks)]


def generate_thumbnail(
    settings,
    image_id: int,
    source: Path,
    side: int,
    *,
    prune: bool = True,
) -> Path:
    """Generate one cached thumbnail atomically and return its cache path."""
    cache = settings.thumbs_dir / f"{image_id}_{side}.png"
    lock = _generation_lock(cache)
    with lock:
        if cache.exists():
            return cache
        try:
            with Image.open(source) as original:
                image = ImageOps.exif_transpose(original)
                if "A" in image.getbands():
                    image = image.convert("RGBA")
                else:
                    image = image.convert("RGB")
        except UnidentifiedImageError:
            image = embeddings.decode_image(source)

        image.thumbnail((side, side))
        fd, tmp_name = tempfile.mkstemp(dir=settings.thumbs_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as file_handle:
                image.save(file_handle, "PNG", optimize=True)
            os.replace(tmp_name, cache)
        except Exception:
            unlink_quiet(tmp_name)
            raise
        if prune:
            prune_thumb_cache(settings)
        return cache


class IdleThumbnailWorker:
    """Generate newest missing thumbnails only while foreground work is idle."""

    def __init__(self, settings, indexer) -> None:
        self.settings = settings
        self.indexer = indexer
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._active_requests = 0
        self._activity_revision = 0
        self._observed_revision = -1
        self._last_activity = time.monotonic()
        self._last_indexer_state: str | None = None
        self._before: tuple[str, int] | None = None
        self._candidates: deque[db.ThumbnailCandidate] = deque()
        self._exhausted = False
        self._cache_count: int | None = None
        self._state = "stopped"
        self._generated = 0
        self._failed = 0

    def start(self) -> None:
        if not self.settings.idle_thumbnails_enabled or self._thread is not None:
            return
        self._state = "waiting"
        self._thread = threading.Thread(
            target=self._run,
            name="idle-thumbnail-worker",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5)
        self._state = "stopped"

    def begin_foreground(self) -> None:
        with self._lock:
            self._active_requests += 1
            self._note_activity_locked()

    def end_foreground(self) -> None:
        with self._lock:
            self._active_requests = max(0, self._active_requests - 1)
            self._note_activity_locked()

    def _note_activity_locked(self) -> None:
        self._last_activity = time.monotonic()
        self._activity_revision += 1

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "state": self._state,
                "generated": self._generated,
                "failed": self._failed,
            }

    def _reset_traversal(self) -> None:
        self._before = None
        self._candidates.clear()
        self._exhausted = False
        self._cache_count = None

    def _foreground_idle(self) -> bool:
        with self._lock:
            return (
                self._active_requests == 0
                and time.monotonic() - self._last_activity >= self.settings.idle_thumbnail_grace_sec
            )

    def _can_work(self) -> bool:
        state = self.indexer.status.get("state", "idle")
        if state != self._last_indexer_state:
            self._last_indexer_state = state
            if state == "idle":
                self._reset_traversal()
        return state == "idle" and self._foreground_idle() and not self._stop.is_set()

    def _refresh_after_activity(self) -> None:
        with self._lock:
            revision = self._activity_revision
        if revision != self._observed_revision:
            self._observed_revision = revision
            self._reset_traversal()

    def _count_cached(self) -> int:
        return sum(1 for _ in self.settings.thumbs_dir.glob("*.png"))

    def _at_capacity(self) -> bool:
        cap = self.settings.thumbs_max_files
        if not cap:
            return False
        if self._cache_count is None:
            self._cache_count = self._count_cached()
        return self._cache_count >= cap

    def _next_candidate(self) -> db.ThumbnailCandidate | None:
        while not self._candidates and not self._exhausted:
            page = db.newest_thumbnail_candidates(
                self.settings.db_path,
                limit=self.settings.idle_thumbnail_query_batch,
                before=self._before,
            )
            if not page:
                self._exhausted = True
                return None
            self._candidates.extend(page)
            last = page[-1]
            self._before = (last.indexed_at, last.id)
        return self._candidates.popleft() if self._candidates else None

    def run_once(self) -> bool:
        """Attempt one thumbnail; return True only when a file was generated."""
        if not self._can_work():
            self._state = "waiting"
            return False
        self._refresh_after_activity()
        if self._at_capacity():
            self._state = "capacity"
            return False

        while self._can_work():
            candidate = self._next_candidate()
            if candidate is None:
                self._state = "complete"
                return False
            cache = self.settings.thumbs_dir / f"{candidate.id}_{self.settings.thumb_size}.png"
            if cache.exists():
                continue
            root = Path(self.settings.store_path).resolve()
            source = (root / candidate.rel_path).resolve()
            if not source.is_relative_to(root) or not source.is_file():
                self._failed += 1
                continue
            self._state = "generating"
            try:
                generate_thumbnail(
                    self.settings,
                    candidate.id,
                    source,
                    self.settings.thumb_size,
                    prune=False,
                )
                self._generated += 1
                if self._cache_count is not None:
                    self._cache_count += 1
                self._state = "waiting"
                return True
            except Exception:  # noqa: BLE001
                self._failed += 1
                log.warning("idle thumbnail generation failed for image %d", candidate.id, exc_info=True)
        self._state = "waiting"
        return False

    def _run(self) -> None:
        try:
            if hasattr(os, "setpriority") and hasattr(os, "PRIO_PROCESS"):
                try:
                    os.setpriority(os.PRIO_PROCESS, 0, 10)
                except OSError:
                    pass
            while not self._stop.is_set():
                generated = self.run_once()
                delay = self.settings.idle_thumbnail_delay_ms / 1000 if generated else 1.0
                self._stop.wait(delay)
        finally:
            self._state = "stopped"


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
