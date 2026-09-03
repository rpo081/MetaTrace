"""Thumbnail cache utilities (single source of truth for LRU pruning)."""
from __future__ import annotations

import concurrent.futures as cf
import logging
import multiprocessing
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
ProcessPoolExecutor = cf.ProcessPoolExecutor


def _generation_lock(path: Path) -> threading.Lock:
    return _generation_locks[hash(path) % len(_generation_locks)]


def _load_thumbnail_source(source: Path):
    try:
        with Image.open(source) as original:
            image = ImageOps.exif_transpose(original)
            if "A" in image.getbands():
                return image.convert("RGBA")
            return image.convert("RGB")
    except UnidentifiedImageError:
        return embeddings.decode_image(source)


def _write_thumbnail_png(image, cache: Path, *, optimize: bool) -> None:
    fd, tmp_name = tempfile.mkstemp(dir=cache.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as file_handle:
            image.save(file_handle, "PNG", optimize=optimize)
        os.replace(tmp_name, cache)
    except Exception:
        unlink_quiet(tmp_name)
        raise


def _bulk_generate_thumbnail_job(
    store_root_str: str,
    thumbs_dir_str: str,
    image_id: int,
    rel_path: str,
    side: int,
) -> str:
    root = Path(store_root_str).resolve()
    source = (root / rel_path).resolve()
    if not source.is_relative_to(root) or not source.is_file():
        return "missing"
    cache = Path(thumbs_dir_str) / f"{image_id}_{side}.png"
    if cache.exists():
        return "exists"
    image = _load_thumbnail_source(source)
    image.thumbnail((side, side))
    _write_thumbnail_png(image, cache, optimize=False)
    return "generated"


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
        image = _load_thumbnail_source(source)

        image.thumbnail((side, side))
        _write_thumbnail_png(image, cache, optimize=True)
        if prune:
            prune_thumb_cache(settings)
        return cache


class BulkThumbnailWorker:
    """Generate missing default thumbnails at full CPU concurrency on demand."""

    def __init__(self, settings, indexer) -> None:
        self.settings = settings
        self.indexer = indexer
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._before: tuple[str, int] | None = None
        self._candidates: deque[db.ThumbnailCandidate] = deque()
        self._exhausted = False
        self._cache_count: int | None = None
        self._state = "stopped"
        self._generated = 0
        self._failed = 0
        self._workers = max(1, int(getattr(settings, "admin_thumbnail_workers", 1)))
        self._foreground_workers = min(
            self._workers,
            max(1, int(getattr(settings, "admin_thumbnail_foreground_workers", 2))),
        )
        self._last_error: str | None = None
        self._active_requests = 0
        self._last_activity = time.monotonic() - settings.idle_thumbnail_grace_sec

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "state": self._state,
                "generated": self._generated,
                "failed": self._failed,
                "workers": self._workers,
                "target_workers": self._target_workers_locked(),
                "last_error": self._last_error,
            }

    def begin_foreground(self) -> None:
        with self._lock:
            self._active_requests += 1
            self._last_activity = time.monotonic()

    def end_foreground(self) -> None:
        with self._lock:
            self._active_requests = max(0, self._active_requests - 1)
            self._last_activity = time.monotonic()

    def is_running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            if self.indexer.status.get("state", "idle") != "idle":
                raise RuntimeError("scan_active")
            self._stop.clear()
            self._before = None
            self._candidates.clear()
            self._exhausted = False
            self._cache_count = None
            self._generated = 0
            self._failed = 0
            self._last_error = None
            self._state = "starting"
            self._thread = threading.Thread(
                target=self._run,
                name="bulk-thumbnail-worker",
                daemon=True,
            )
            self._thread.start()
            return True

    def stop(self) -> bool:
        with self._lock:
            thread = self._thread
            if thread is None or not thread.is_alive():
                self._state = "stopped"
                return False
            self._state = "stopping"
            self._stop.set()
        thread.join(timeout=5)
        return True

    def _set_state(self, state: str, *, error: str | None = None) -> None:
        with self._lock:
            self._state = state
            if error is not None:
                self._last_error = error

    def _target_workers_locked(self) -> int:
        if self._active_requests > 0:
            return 1
        if time.monotonic() - self._last_activity < self.settings.idle_thumbnail_grace_sec:
            return self._foreground_workers
        return self._workers

    def _target_workers(self) -> int:
        with self._lock:
            return self._target_workers_locked()

    def _count_cached(self) -> int:
        return sum(1 for _ in self.settings.thumbs_dir.glob("*.png"))

    def _remaining_capacity(self, pending_count: int) -> int | None:
        cap = self.settings.thumbs_max_files
        if not cap:
            return None
        if self._cache_count is None:
            self._cache_count = self._count_cached()
        return max(0, cap - self._cache_count - pending_count)

    def _next_candidate(self) -> db.ThumbnailCandidate | None:
        while not self._candidates and not self._exhausted:
            page = db.newest_thumbnail_candidates(
                self.settings.db_path,
                limit=max(self.settings.idle_thumbnail_query_batch, self._workers * 4),
                before=self._before,
            )
            if not page:
                self._exhausted = True
                return None
            self._candidates.extend(page)
            last = page[-1]
            self._before = (last.indexed_at, last.id)
        return self._candidates.popleft() if self._candidates else None

    def _submit_ready_candidates(self, executor, pending: dict[cf.Future, int]) -> None:
        target_workers = self._target_workers()
        while not self._stop.is_set() and len(pending) < target_workers:
            remaining_capacity = self._remaining_capacity(len(pending))
            if remaining_capacity is not None and remaining_capacity <= 0:
                self._set_state("capacity")
                return
            candidate = self._next_candidate()
            if candidate is None:
                return
            cache = self.settings.thumbs_dir / f"{candidate.id}_{self.settings.thumb_size}.png"
            if cache.exists():
                continue
            future = executor.submit(
                _bulk_generate_thumbnail_job,
                str(self.settings.store_path),
                str(self.settings.thumbs_dir),
                candidate.id,
                candidate.rel_path,
                self.settings.thumb_size,
            )
            pending[future] = candidate.id

    def _handle_future_result(self, result: str) -> None:
        with self._lock:
            if result == "generated":
                self._generated += 1
                if self._cache_count is not None:
                    self._cache_count += 1
            elif result == "missing":
                self._failed += 1

    def _run(self) -> None:
        executor = None
        pending: dict[cf.Future, int] = {}
        try:
            self._set_state("running")
            executor = ProcessPoolExecutor(
                max_workers=self._workers,
                mp_context=multiprocessing.get_context("spawn"),
            )
            while not self._stop.is_set():
                self._submit_ready_candidates(executor, pending)
                if not pending:
                    if self._state == "capacity":
                        break
                    if self._exhausted:
                        self._set_state("complete")
                        break
                    time.sleep(0.05)
                    continue
                if len(pending) > self._target_workers():
                    time.sleep(0.05)
                    continue
                done, _ = cf.wait(pending, timeout=0.2, return_when=cf.FIRST_COMPLETED)
                for future in done:
                    pending.pop(future, None)
                    try:
                        result = future.result()
                    except Exception as exc:  # noqa: BLE001
                        with self._lock:
                            self._failed += 1
                            self._last_error = str(exc)
                        log.warning("bulk thumbnail generation failed", exc_info=True)
                        continue
                    self._handle_future_result(result)
            if self._stop.is_set():
                self._set_state("stopped")
        except Exception as exc:  # noqa: BLE001
            log.warning("bulk thumbnail worker crashed", exc_info=True)
            self._set_state("error", error=str(exc))
        finally:
            if executor is not None:
                for future in pending:
                    future.cancel()
                executor.shutdown(wait=False, cancel_futures=True)
            prune_thumb_cache(self.settings)
            with self._lock:
                self._thread = None
                if self._state == "starting":
                    self._state = "stopped"


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
        self._bulk_worker = None

    def set_bulk_worker(self, worker) -> None:
        self._bulk_worker = worker

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
        bulk_running = self._bulk_worker is not None and self._bulk_worker.is_running()
        return state == "idle" and self._foreground_idle() and not bulk_running and not self._stop.is_set()

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
