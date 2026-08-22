"""Filesystem scanner and incremental embedding indexer (SQLite + FAISS)."""
from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import db, embeddings, metadata
from .config import Settings

log = logging.getLogger(__name__)

# Files whose (size, mtime) differ by less than this are treated as unchanged.
MTIME_TOLERANCE_SEC = 2.0


def _quarantine_corrupt_index(index_file: Path) -> Path:
    """Rename an unreadable index file out of the way so a rebuild can proceed."""
    quarantine = index_file.with_suffix(".faiss.corrupt")
    n = 1
    while quarantine.exists():  # keep every corrupt generation
        quarantine = index_file.with_suffix(f".faiss.corrupt.{n}")
        n += 1
    index_file.rename(quarantine)
    return quarantine


@dataclass
class ScanReport:
    trigger: str = ""
    started_at: float = 0.0
    duration_sec: float = 0.0
    seen: int = 0
    added: int = 0
    updated: int = 0
    removed: int = 0
    unchanged: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        """API-safe report: error *count* only — no per-file paths/strings.

        Full error strings stay server-side; they are logged at the moment
        they occur during the scan.
        """
        d = self.__dict__.copy()
        d.pop("errors")
        d["error_count"] = len(self.errors)
        return d


@dataclass(frozen=True)
class DiskFile:
    rel_path: str
    abs_path: Path
    size: int
    mtime: float


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(chunk_size):
            h.update(chunk)
    return h.hexdigest()


class Indexer:
    """Owns the FAISS index; all mutations happen under an internal RLock.

    Concurrency model: scans build/mutate a *working copy* of the index and
    swap it into ``self.index`` atomically under ``_lock`` at the end. Readers
    may snapshot the reference under ``lock`` and use it without holding the
    lock — a swapped-out index object is never mutated again.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self._lock = threading.RLock()
        # Serializes scans against each other (searches stay lock-free).
        self._scan_lock = threading.Lock()
        self.index = None  # faiss.IndexIDMap2, created lazily on first batch
        self.status: dict = {"state": "idle", "last_report": None}

    # ------------------------------------------------------------------ api
    @property
    def count(self) -> int:
        return int(self.index.ntotal) if self.index is not None else 0

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def load_or_create(self) -> None:
        """Load the persisted index, reconciling it against the database.

        Self-heal: a missing or corrupt index file, or an ntotal/DB-row-count
        mismatch, forces a full rebuild on the next scan (DB is reset so every
        store file is re-embedded). A corrupt file is quarantined as
        ``index.faiss.corrupt`` instead of crashing startup.
        """
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

    def incremental(
        self,
        trigger: str = "manual",
        force_rebuild: bool = False,
        progress=None,
    ) -> ScanReport:
        """Scan the store, embed new/changed files, drop deleted ones."""
        report = ScanReport(trigger=trigger, started_at=time.time())
        with self._scan_lock:
            self._set_state("scanning")
            try:
                self._scan(report, force_rebuild, progress)
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

    def _force_full_rebuild(self, reason: str) -> None:
        """Reset DB + in-memory index so the next scan re-embeds everything."""
        log.warning("forcing full index rebuild (%s)", reason)
        db.reset(self.settings.db_path)
        with self._lock:
            self.index = None

    def _reconcile_counts(self) -> None:
        """Compare FAISS ntotal vs DB row count; force rebuild on mismatch.

        Catches vectors lost to crashes between the DB upsert and add_with_ids
        (or any other divergence window) before they become silent search holes.
        """
        s = self.settings
        assert self.index is not None
        ntotal = int(self.index.ntotal)
        n_db = db.count(s.db_path)
        if ntotal != n_db:
            log.warning(
                "index/DB mismatch detected: faiss.ntotal=%d but db rows=%d; "
                "forcing full rebuild to restore parity",
                ntotal, n_db,
            )
            self._force_full_rebuild("index/DB count mismatch")

    def _walk_store(self) -> dict[str, DiskFile]:
        found: dict[str, DiskFile] = {}
        root = Path(self.settings.store_path)
        exts = self.settings.extensions
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            dp = Path(dirpath)
            for name in sorted(filenames):
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

    def _scan(self, report: ScanReport, force_rebuild: bool, progress) -> None:
        import faiss

        s = self.settings
        db.init_db(s.db_path)
        disk = self._walk_store()
        report.seen = len(disk)
        known = db.list_entries(s.db_path)

        stored_model = db.kv_get(s.db_path, "model")
        current_model = f"{s.model_name}:{s.model_pretrained}"
        if stored_model and stored_model != current_model:
            log.warning("embedding model changed %s -> %s; rebuilding index",
                        stored_model, current_model)
            force_rebuild = True
        if force_rebuild:
            db.reset(s.db_path)
            known = {}
            working = None
        else:
            # Snapshot the current index as our private working copy — CLONED,
            # never the published object itself. FAISS releases the GIL inside
            # remove_ids/add_with_ids, so mutating the object that lock-free
            # readers are querying is undefined behavior (crashes). The clone
            # is cheap at this scale (20k x 512 float32 ≈ 40 MB) and the
            # published generation stays frozen until the atomic swap below.
            with self._lock:
                working = (
                    faiss.clone_index(self.index) if self.index is not None else None
                )
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

        if removed_ids:
            if working is not None:
                working.remove_ids(np.array(removed_ids, dtype="int64"))
            db.remove_ids(s.db_path, removed_ids)
            report.removed = len(removed_ids)

        # Drop stale vectors of updated files before re-adding them.
        update_ids = sorted(known[f.rel_path].id for f in to_update)
        if update_ids and working is not None:
            working.remove_ids(np.array(update_ids, dtype="int64"))

        pending = to_add + to_update
        added_rel_paths = {f.rel_path for f in to_add}
        batch = max(1, s.batch_size)
        for i in range(0, len(pending), batch):
            chunk = pending[i : i + batch]
            working = self._process_chunk(chunk, added_rel_paths, report, working)
            if progress:
                progress(report.as_dict())

        if working is not None:
            tmp = s.index_file.with_suffix(".faiss.tmp")
            faiss.write_index(working, str(tmp))
            tmp.replace(s.index_file)
        # Publish the rebuilt index atomically; concurrent searches that hold
        # the old reference finish safely against the previous generation.
        with self._lock:
            self.index = working
        db.kv_set(s.db_path, "last_scan", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        log.info(
            "scan done (%s): seen=%d added=%d updated=%d removed=%d failed=%d in %.1fs",
            report.trigger, report.seen, report.added, report.updated,
            report.removed, report.failed, report.duration_sec or 0.0,
        )

    def _process_chunk(
        self,
        chunk: list[DiskFile],
        added_set: set[str],
        report: ScanReport,
        index,
    ):
        """Embed one chunk into ``index`` (created on first use) and return it."""
        import faiss

        s = self.settings
        images: list = []
        ok_files: list[DiskFile] = []
        shas: dict[str, str] = {}
        dims: dict[str, tuple[int, int]] = {}
        for f in chunk:
            try:
                img = embeddings.decode_image(f.abs_path)
                images.append(img)
                ok_files.append(f)
                shas[f.rel_path] = sha256_file(f.abs_path)
                dims[f.rel_path] = img.size  # (width, height)
            except Exception as exc:  # noqa: BLE001 - one bad file must not kill the scan
                report.failed += 1
                report.errors.append(f"{f.rel_path}: {exc}")
                log.warning("decode failed: %s (%s)", f.rel_path, exc)
        if not images:
            return index

        xmp_map = metadata.extract_xmp([f.abs_path for f in ok_files])

        try:
            vectors = embeddings.embed_images(images, s)
        except Exception as exc:  # noqa: BLE001
            report.failed += len(ok_files)
            report.errors.append(f"embed batch failed: {exc}")
            log.exception("embed batch failed")
            return index

        if index is None:
            dim = int(vectors.shape[1])
            log.info("creating flat IP index (dim=%d)", dim)
            index = faiss.IndexIDMap2(faiss.IndexFlatIP(dim))

        ids = []
        n_added = n_updated = 0
        for f, _vec in zip(ok_files, vectors):
            width, height = dims[f.rel_path]
            row_id = db.upsert_image(
                s.db_path,
                rel_path=f.rel_path,
                original_path=s.original_path_for(f.rel_path),
                size=f.size,
                mtime=f.mtime,
                sha256=shas[f.rel_path],
                width=width,
                height=height,
                xmp=xmp_map.get(str(f.abs_path), {}),
            )
            ids.append(row_id)
            if f.rel_path in added_set:
                n_added += 1
            else:
                n_updated += 1
        index.add_with_ids(
            vectors.astype("float32"), np.array(ids, dtype="int64")
        )
        report.added += n_added
        report.updated += n_updated
        return index


__all__ = ["Indexer", "ScanReport", "DiskFile", "sha256_file"]
