"""FAISS vector store — extracted from indexer.py.

Owns the on-disk index lifecycle: quarantine, atomic publish, and lock-free
clone handling. All FAISS imports remain lazy (faiss releases the GIL — concurrent
read+write is UB, so scans clone and swap atomically under ``_lock``).
"""
from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Protocol, runtime_checkable

import numpy as np

log = logging.getLogger(__name__)


def _quarantine_corrupt_index(index_file: Path) -> Path:
    """Rename an unreadable index file out of the way so a rebuild can proceed.

    Uses ``with_name`` instead of ``with_suffix`` — ``with_suffix`` replaces only
    the final suffix, so ``index.faiss`` → ``index.faiss.corrupt.1`` would be
    fragile for multi-dot names. ``with_name(f"{name}.corrupt")`` appends reliably.
    """
    quarantine = index_file.with_name(f"{index_file.name}.corrupt")
    n = 1
    while quarantine.exists():  # keep every corrupt generation
        quarantine = index_file.with_name(f"{index_file.name}.corrupt.{n}")
        n += 1
    index_file.rename(quarantine)
    return quarantine


@runtime_checkable
class VectorStore(Protocol):
    """Minimal protocol for FAISS index operations used by Indexer/Scanner."""

    @property
    def ntotal(self) -> int:  # pragma: no cover
        ...

    def add_with_ids(self, vectors: np.ndarray, ids: np.ndarray) -> None:  # pragma: no cover
        ...

    def remove_ids(self, ids: np.ndarray) -> None:  # pragma: no cover
        ...

    def search(self, vectors: np.ndarray, k: int):  # pragma: no cover
        ...

    def clone(self):  # pragma: no cover
        """Return a deep copy (wraps ``faiss.clone_index``)."""
        ...


class FaissStore:
    """Wraps a ``faiss.IndexIDMap2`` with atomic persistence and locking.

    Concurrency model: scans build/mutate a *working copy* of the index and
    swap it into ``self.index`` atomically under ``_lock`` at the end. Readers
    may snapshot the reference under ``lock`` and use it without holding the
    lock — a swapped-out index object is never mutated again.
    """

    def __init__(self, settings) -> None:
        self.settings = settings
        self._lock = threading.RLock()
        self.index = None  # faiss.IndexIDMap2, created lazily on first batch
        self._last_publish_monotonic = float("-inf")

    # ------------------------------------------------------------------ props
    @property
    def count(self) -> int:
        return int(self.index.ntotal) if self.index is not None else 0

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    @property
    def ntotal(self) -> int:
        return self.count

    # ---------------------------------------------------------------- delegates
    def add_with_ids(self, vectors: np.ndarray, ids: np.ndarray) -> None:
        if self.index is None:
            raise RuntimeError("index not initialized")
        self.index.add_with_ids(vectors, ids)

    def remove_ids(self, ids: np.ndarray) -> None:
        if self.index is not None:
            self.index.remove_ids(ids)

    def search(self, vectors: np.ndarray, k: int):
        if self.index is None:
            raise RuntimeError("index not initialized")
        return self.index.search(vectors, k)

    def clone(self):
        return self.clone_published()

    # --------------------------------------------------------------- lifecycle
    def clone_index(self, index):
        """Clone any index instance (or None) via ``faiss.clone_index``."""
        import faiss

        return faiss.clone_index(index) if index is not None else None

    def clone_published_index(self):
        import faiss

        with self._lock:
            return faiss.clone_index(self.index) if self.index is not None else None

    # Alias for protocol compatibility / ergonomic use
    clone_published = clone_published_index

    def publish_index(self, index) -> None:
        """Atomically persist *index* and swap it into ``self.index``."""
        import faiss

        if index is None:
            self.settings.index_file.unlink(missing_ok=True)
            with self._lock:
                self.index = None
            return
        tmp = self.settings.index_file.with_suffix(".faiss.tmp")
        self.settings.data_path.mkdir(parents=True, exist_ok=True)
        faiss.write_index(index, str(tmp))
        # durability: fsync file before atomic replace so a crash
        # cannot leave a half-written index (critical at 200k ~400MB)
        try:
            with open(tmp, "rb") as fh:
                os.fsync(fh.fileno())
        except OSError:
            log.warning("fsync failed for %s", tmp, exc_info=True)
        tmp.replace(self.settings.index_file)
        # fsync directory to persist the rename on POSIX
        try:
            dir_fd = os.open(str(self.settings.data_path), os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
        with self._lock:
            self.index = index

    # alias used by facade
    publish = publish_index

    def publish_progress(self, index, interval_sec: float) -> None:
        """Swap+persist the working index during long scans (throttled).

        Keeps the on-disk/published state close to the per-chunk DB commits so
        an interrupted scan loses at most ~interval of work instead of forcing
        a full redo. A copy is published; the caller's object stays private
        and mutable.
        """
        import faiss
        import time

        now = time.monotonic()
        if now - self._last_publish_monotonic < interval_sec:
            return
        self._last_publish_monotonic = now
        self.publish_index(faiss.clone_index(index))


__all__ = ["VectorStore", "FaissStore", "_quarantine_corrupt_index"]
