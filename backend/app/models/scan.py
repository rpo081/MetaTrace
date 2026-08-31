"""Scan data models (pure data, no I/O). Extracted from indexer.py for testability."""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from pathlib import Path


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
        """API-safe report: error *count* only — no per-file paths/strings."""
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


@dataclass(frozen=True)
class ImageDTO:
    """DTO for ``images`` table rows — replaces leaked ``sqlite3.Row``.

    Use attributes, not ``row['col']``. Provides ``__getitem__`` for backwards
    compatibility during migration (maps to compile guard).
    """

    id: int
    rel_path: str
    original_path: str
    size: int
    mtime: float
    sha256: str | None
    width: int | None
    height: int | None
    xmp: dict
    indexed_at: str

    def __getitem__(self, key: str):  # pragma: no cover - compat shim
        if key == "xmp":
            import json as _json

            # Legacy tests expect JSON string; attribute holds dict
            return _json.dumps(getattr(self, "xmp"))
        return getattr(self, key)

    def get(self, key: str, default=None):  # pragma: no cover
        if key == "xmp":
            import json as _json

            val = getattr(self, "xmp", default)
            return _json.dumps(val) if isinstance(val, dict) else val
        return getattr(self, key, default)

    def keys(self):  # pragma: no cover
        return ["id", "rel_path", "original_path", "size", "mtime", "sha256", "width", "height", "xmp", "indexed_at"]

    def __contains__(self, key: object):  # pragma: no cover
        return key in self.keys()


def row_to_dto(row) -> ImageDTO:
    """Convert a sqlite3.Row or mapping to ImageDTO."""
    import json as _json

    xmp_val = row["xmp"] if "xmp" in row.keys() else row.get("xmp", "{}")  # type: ignore
    if isinstance(xmp_val, str):
        try:
            xmp = _json.loads(xmp_val) if xmp_val else {}
        except Exception:
            xmp = {}
    elif isinstance(xmp_val, dict):
        xmp = xmp_val
    else:
        xmp = {}
    return ImageDTO(
        id=int(row["id"]),
        rel_path=str(row["rel_path"]),
        original_path=str(row["original_path"]),
        size=int(row["size"]),
        mtime=float(row["mtime"]),
        sha256=row["sha256"],
        width=row["width"],
        height=row["height"],
        xmp=xmp,
        indexed_at=str(row["indexed_at"]),
    )


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(chunk_size):
            h.update(chunk)
    return h.hexdigest()


def snapshot_meta_to_disk_file(store_root: Path, rel_path: str, meta) -> DiskFile | None:
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
