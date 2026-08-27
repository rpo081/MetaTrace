"""Filesystem scanner and incremental embedding indexer (SQLite + FAISS)."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from collections import Counter, deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import db, embeddings, metadata
from .config import Settings

log = logging.getLogger(__name__)

# Files whose (size, mtime) differ by less than this are treated as unchanged.
MTIME_TOLERANCE_SEC = 2.0
SCAN_CHECKPOINT_VERSION = 1
# Publish (swap + persist) a long-running scan's working index at least this
# often, so a crash costs at most ~interval worth of work instead of losing
# the whole scan (the DB commits per chunk; without periodic publishes any
# interruption left the DB permanently ahead of the published index).
PUBLISH_INTERVAL_SEC = 30.0
# Scan-time decode downscales to this max side before the image enters the
# prefetch window / chunk list. CLIP preprocesses to 224 px; original
# dimensions are captured before downscaling and stored in the DB.
SCAN_DECODE_MAX_SIDE = 512


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
    paused_duration_sec: float = 0.0
    seen: int = 0
    processed: int = 0
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

        if self.duration_sec > 0:
            elapsed = max(0.0, self.duration_sec - self.paused_duration_sec)
        elif self.started_at > 0:
            elapsed = max(0.0, time.time() - self.started_at - self.paused_duration_sec)
        else:
            elapsed = 0.0

        d["elapsed_sec"] = round(elapsed, 1)

        indexed = self.added + self.updated
        if elapsed > 0:
            d["scans_per_min"] = round((self.processed / elapsed) * 60, 1)
            d["embeddings_per_min"] = round((indexed / elapsed) * 60, 1)
            d["scans_per_sec"] = round(self.processed / elapsed, 2)
            d["embeddings_per_sec"] = round(indexed / elapsed, 2)
        else:
            d["scans_per_min"] = 0.0
            d["embeddings_per_min"] = 0.0
            d["scans_per_sec"] = 0.0
            d["embeddings_per_sec"] = 0.0

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


def _snapshot_meta_to_disk_file(store_root: Path, rel_path: str, meta) -> DiskFile | None:
    if isinstance(meta, dict):
        mtime = meta.get("mtime")
        size = meta.get("size")
    elif isinstance(meta, (list, tuple)) and len(meta) >= 2:
        mtime, size = meta[0], meta[1]
    else:
        return None
    if mtime is None or size is None:
        return None
    normalized = rel_path.replace("\\", "/")
    return DiskFile(
        rel_path=normalized,
        abs_path=store_root / Path(normalized),
        size=int(size),
        mtime=float(mtime),
    )


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
        self._pause_gate = threading.Event()
        self._pause_gate.set()
        self._resume_checkpoint: dict | None = None
        self.index = None  # faiss.IndexIDMap2, created lazily on first batch
        self.status: dict = {"state": "idle", "last_report": None, "inventory_source": None}
        # Progress-publish throttle for long scans (see _publish_progress).
        self._last_publish_monotonic = float("-inf")
        # Worker threads spawn lazily on first submit; scans are serialized by
        # _scan_lock, so the pools never run concurrently.
        self._decode_pool = ThreadPoolExecutor(
            max_workers=max(1, settings.decode_workers),
            thread_name_prefix="metatrace-decode",
        )
        self._xmp_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="metatrace-xmp")

    # ------------------------------------------------------------------ api
    @property
    def count(self) -> int:
        return int(self.index.ntotal) if self.index is not None else 0

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def load_or_create(self) -> None:
        """Load the persisted index, reconciling it against the database.

        Self-heal: a missing or corrupt index file is quarantined as
        ``index.faiss.corrupt`` and forces a full rebuild (no vectors exist to
        repair). A readable index is *repaired surgically*: vector ids that
        have no DB row are removed, DB rows that have no vector are pruned so
        the next scan re-embeds exactly those files — an interrupted scan
        costs at most its last chunk, never a full rebuild.
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
        self._load_resume_checkpoint()

    def incremental(
        self,
        trigger: str = "manual",
        force_rebuild: bool = False,
        progress=None,
        delta_info: dict | None = None,
    ) -> ScanReport:
        """Scan the store, embed new/changed files, drop deleted ones.
        
        Args:
            trigger: Scan reason (e.g., 'manual-api', 'startup')
            force_rebuild: Force complete index rebuild
            progress: Progress callback
            delta_info: Optional delta dict with changes (created/deleted/modified lists)
                       If provided and not force_rebuild, only process these changes.
        """
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
                # Try delta-optimized path if available and not forcing rebuild
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
        """Pause the active scan at the next cooperative checkpoint."""
        if self.status["state"] != "scanning":
            return False
        self._pause_gate.clear()
        return True

    def resume(self) -> bool:
        """Resume a paused scan."""
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

    @property
    def _resume_checkpoint_file(self) -> Path:
        return self.settings.data_path / "scan_checkpoint.json"

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

    def _read_resume_checkpoint(self) -> dict | None:
        path = self._resume_checkpoint_file
        if not path.exists():
            return None
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not read scan checkpoint: %s", exc)
            return None
        if data.get("version") != SCAN_CHECKPOINT_VERSION:
            log.warning("ignoring unknown scan checkpoint version: %r", data.get("version"))
            return None
        if data.get("model") != self._model_key():
            log.warning("discarding scan checkpoint for different model: %s", data.get("model"))
            self._clear_resume_checkpoint()
            return None
        return data

    def _write_resume_checkpoint(self, payload: dict) -> None:
        path = self._resume_checkpoint_file
        tmp = path.with_suffix(path.suffix + ".tmp")
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path)
        self._resume_checkpoint = payload

    def _clear_resume_checkpoint(self) -> None:
        self._resume_checkpoint = None
        self._resume_checkpoint_file.unlink(missing_ok=True)

    def _save_planning_checkpoint(
        self,
        mode: str,
        force_rebuild: bool,
        report: ScanReport,
        delta_info: dict | None = None,
    ) -> None:
        payload = {
            "version": SCAN_CHECKPOINT_VERSION,
            "phase": "planning",
            "mode": mode,
            "force_rebuild": force_rebuild,
            "trigger": report.trigger,
            "report": report.as_dict(),
            "delta_info": delta_info,
            "model": self._model_key(),
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        self._write_resume_checkpoint(payload)

    def _save_pending_checkpoint(
        self,
        mode: str,
        force_rebuild: bool,
        report: ScanReport,
        remaining_rel_paths: list[str],
        remaining_added_rel_paths: set[str],
    ) -> None:
        payload = {
            "version": SCAN_CHECKPOINT_VERSION,
            "phase": "pending",
            "mode": mode,
            "force_rebuild": force_rebuild,
            "trigger": report.trigger,
            "report": report.as_dict(),
            "remaining_rel_paths": remaining_rel_paths,
            "remaining_added_rel_paths": sorted(
                rel_path for rel_path in remaining_rel_paths if rel_path in remaining_added_rel_paths
            ),
            "model": self._model_key(),
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        self._write_resume_checkpoint(payload)

    def _load_resume_checkpoint(self) -> None:
        checkpoint = self._read_resume_checkpoint()
        if checkpoint is None:
            return
        if checkpoint.get("phase") != "pending":
            # A "planning" checkpoint means the previous scan died before
            # finishing its first batch — resuming would redo everything
            # anyway. Discard it instead of booting into a phantom "paused"
            # state that also blocks RUN_INITIAL_SCAN_ON_START (trigger_now
            # refuses while paused).
            log.info(
                "discarding stale %r-phase scan checkpoint (nothing processed yet)",
                checkpoint.get("phase"),
            )
            self._clear_resume_checkpoint()
            return
        self._resume_checkpoint = checkpoint
        self._pause_gate.clear()
        self._set_state("paused")
        self.status["last_report"] = checkpoint.get("report")
        log.info("found paused scan checkpoint with %d remaining file(s)", len(checkpoint.get("remaining_rel_paths", [])))

    def _report_from_dict(self, data: dict) -> ScanReport:
        report = ScanReport(trigger=str(data.get("trigger", "resume")))
        report.started_at = float(data.get("started_at", time.time()))
        report.duration_sec = float(data.get("duration_sec", 0.0))
        report.paused_duration_sec = float(data.get("paused_duration_sec", 0.0))
        report.seen = int(data.get("seen", 0))
        report.processed = int(data.get("processed", 0))
        report.added = int(data.get("added", 0))
        report.updated = int(data.get("updated", 0))
        report.removed = int(data.get("removed", 0))
        report.unchanged = int(data.get("unchanged", 0))
        report.failed = int(data.get("failed", 0))
        return report

    def _clone_published_index(self):
        import faiss

        with self._lock:
            return faiss.clone_index(self.index) if self.index is not None else None

    def _publish_index(self, index) -> None:
        import faiss

        if index is None:
            self.settings.index_file.unlink(missing_ok=True)
            with self._lock:
                self.index = None
            return
        tmp = self.settings.index_file.with_suffix(".faiss.tmp")
        faiss.write_index(index, str(tmp))
        tmp.replace(self.settings.index_file)
        with self._lock:
            self.index = index

    def _disk_files_for_rel_paths(self, rel_paths: list[str], report: ScanReport) -> list[DiskFile]:
        pending: list[DiskFile] = []
        for rel_path in rel_paths:
            abs_path = self.settings.store_path / rel_path
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

    def _disk_files_from_snapshot(self, rel_paths: list[str], report: ScanReport) -> list[DiskFile] | None:
        snapshot = self._load_snapshot_inventory()
        if snapshot is None:
            return None
        pending: list[DiskFile] = []
        for rel_path in rel_paths:
            disk_file = snapshot.get(rel_path.replace("\\", "/"))
            if disk_file is None:
                return None
            pending.append(disk_file)
        return pending

    def _disk_file_from_live_stat(self, rel_path: str, report: ScanReport) -> DiskFile | None:
        abs_path = self.settings.store_path / rel_path
        try:
            stat = abs_path.stat()
        except OSError as exc:
            report.failed += 1
            report.processed += 1
            report.errors.append(f"{rel_path}: {exc}")
            log.warning("delta stat failed: %s (%s)", rel_path, exc)
            return None
        return DiskFile(
            rel_path=rel_path,
            abs_path=abs_path,
            size=stat.st_size,
            mtime=stat.st_mtime,
        )

    def _disk_file_for_delta_path(
        self,
        rel_path: str,
        snapshot: dict[str, DiskFile] | None,
        report: ScanReport,
    ) -> DiskFile | None:
        normalized = rel_path.replace("\\", "/")
        if snapshot is not None:
            disk_file = snapshot.get(normalized)
            if disk_file is not None:
                return disk_file
            log.warning("delta path missing from store snapshot, falling back to live stat: %s", normalized)
        return self._disk_file_from_live_stat(normalized, report)

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

        # Publish a COPY of the working index: callers keep mutating ``working``
        # after resume, and a published index object must never be mutated
        # again (lock-free search readers may hold it; FAISS releases the GIL).
        self._publish_index(faiss.clone_index(working) if working is not None else None)
        self._save_pending_checkpoint(
            mode,
            force_rebuild,
            report,
            [f.rel_path for f in remaining],
            added_rel_paths,
        )

    def _publish_progress(self, index) -> None:
        """Swap+persist the working index during long scans (throttled).

        Keeps the on-disk/published state close to the per-chunk DB commits so
        an interrupted scan loses at most ~PUBLISH_INTERVAL_SEC of work instead
        of forcing a full redo. A copy is published; the caller's object stays
        private and mutable.
        """
        import faiss

        now = time.monotonic()
        if now - self._last_publish_monotonic < PUBLISH_INTERVAL_SEC:
            return
        self._last_publish_monotonic = now
        self._publish_index(faiss.clone_index(index))

    def _force_full_rebuild(self, reason: str) -> None:
        """Reset DB + in-memory index so the next scan re-embeds everything."""
        log.warning("forcing full index rebuild (%s)", reason)
        db.reset(self.settings.db_path)
        with self._lock:
            self.index = None

    def _reconcile_counts(self) -> None:
        """Make the FAISS id set and the DB id set match at startup.

        Replaces the old count-compare + full-rebuild behavior: an interrupted
        scan commits DB rows per chunk but used to publish the index only at
        the end, so *any* interruption permanently diverged DB and index and
        every restart wiped everything (rescan-from-zero loop). Repair instead:

        - orphan vectors (id without DB row): remove_ids from the index
        - hole rows (DB row without vector): delete row; the next incremental
          scan re-embeds exactly those files as additions

        Both directions keep search honest (no silent holes); a crash now
        costs at most one chunk of work. Runs before the server starts serving,
        so mutating self.index in place is safe here.
        """
        import faiss

        s = self.settings
        assert self.index is not None
        entries = db.list_entries(s.db_path)
        db_ids = {e.id for e in entries.values()}
        id_array = faiss.vector_to_array(self.index.id_map)
        index_ids = {int(i) for i in id_array}

        # Duplicate vectors under one id (pre-fix resume replays) are invisible
        # to set comparison: ntotal > unique ids. Drop ALL copies of affected
        # ids and their rows; the next scan re-embeds those files cleanly.
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

    def _walk_store(self, pause=None) -> dict[str, DiskFile]:
        found: dict[str, DiskFile] = {}
        root = Path(self.settings.store_path)
        exts = self.settings.extensions
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

    def _load_snapshot_inventory(self) -> dict[str, DiskFile] | None:
        if not self.settings.use_store_snapshot_for_initial_scan:
            return None
        path = self.settings.latest_store_snapshot_file
        if not path.exists():
            return None
        try:
            with open(path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not read store snapshot %s: %s", path.name, exc)
            return None

        entries = payload.get("files") if isinstance(payload, dict) and "files" in payload else payload
        if not isinstance(entries, dict):
            log.warning("ignoring invalid store snapshot payload in %s", path.name)
            return None

        disk: dict[str, DiskFile] = {}
        for rel_path, meta in entries.items():
            if Path(rel_path).suffix.lower() not in self.settings.extensions:
                continue
            disk_file = _snapshot_meta_to_disk_file(self.settings.store_path, rel_path, meta)
            if disk_file is None:
                log.warning("ignoring invalid store snapshot entry for %s", rel_path)
                continue
            disk[disk_file.rel_path] = disk_file

        if not disk:
            log.warning("store snapshot %s contained no usable image entries", path.name)
            return None
        log.info("using store snapshot %s for inventory (%d files)", path.name, len(disk))
        self._set_inventory_source("snapshot")
        return disk

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
        db.kv_set(s.db_path, "last_scan", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        log.info(
            "scan done (%s): seen=%d added=%d updated=%d removed=%d failed=%d in %.1fs",
            report.trigger, report.seen, report.added, report.updated,
            report.removed, report.failed, report.duration_sec or 0.0,
        )

    def _process_delta(self, report: ScanReport, delta_info: dict, progress) -> None:
        """Delta-optimized scan: only process changed files (created/deleted/modified).
        
        This is much faster than _scan() because:
        - No _walk_store() call (no full filesystem traversal)
        - Only processes the delta file lists
        - Minimal DB lookups
        """
        s = self.settings
        db.init_db(s.db_path, configure_journal=False)
        known = db.list_entries(s.db_path)

        changes = delta_info.get("changes", {})
        deleted_paths = set(changes.get("deleted", []))
        created_paths = set(changes.get("created", []))
        modified_paths = set(changes.get("modified", []))
        snapshot = self._load_snapshot_inventory()

        # Report seen = total changes (not full store size)
        report.seen = len(deleted_paths) + len(created_paths) + len(modified_paths)

        # Snapshot the current index for mutation
        working = self._clone_published_index()

        # === STEP 1: Handle deleted files ===
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
                # Debug: show what we have in known
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

        # === STEP 2: Handle created + modified files ===
        # Prefer snapshot metadata so delta scans avoid per-file store stats.
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
        
        # Remove stale vectors for modified files before re-adding
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

        # === STEP 3: Publish updated index ===
        self._publish_index(working)
        log.info("published index to self.index, new ntotal=%d", (working.ntotal if working else 0))
        
        db.kv_set(s.db_path, "last_scan", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        
        # Clean up the delta file after successful processing so it's not re-processed
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
        img = embeddings.decode_image(f.abs_path)
        dims = img.size  # original (width, height) for the DB row
        # Downscale in the worker: CLIP consumes 224 px inputs, so shipping
        # 100+ Mpixel frames (hundreds of MB RGB each) through the prefetch
        # window and the per-chunk accumulation list OOM-kills the container.
        img.thumbnail((SCAN_DECODE_MAX_SIDE, SCAN_DECODE_MAX_SIDE))
        return f, img, sha256_file(f.abs_path), dims

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
        """Embed one chunk into ``index`` (created on first use) and return it.

        Decode+hash run on a thread pool behind a bounded in-flight window
        (memory guard); XMP extraction overlaps the embedding step as a
        background future. Pause checkpoints stay per-file on the consuming
        side, so snapshots/resume behave exactly like the serial version.
        """
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
                fut = self._decode_pool.submit(self._decode_and_hash, chunk[next_offset])
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
                dims[done_file.rel_path] = dims_xy  # original (width, height)
            except Exception as exc:  # noqa: BLE001 - one bad file must not kill the scan
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

        # Remove pre-existing vectors for EVERY file that already has a DB row
        # — not only planned updates. Replayed work (resume from a stale
        # checkpoint, re-applied delta) can list files as "added" that were
        # already committed+published by the crashed run; skipping removal for
        # those produced two vectors under one id (silent search dupes).
        # remove_ids is a no-op for genuinely new files, so this is cheap and
        # makes re-processing idempotent.
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


__all__ = ["Indexer", "ScanReport", "DiskFile", "sha256_file"]
