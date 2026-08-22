"""Embedded XMP metadata extraction via exiftool."""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Sequence

log = logging.getLogger(__name__)

BATCH_SIZE = 64  # files per exiftool invocation
# Per-invocation cap. Worst case per 64-file chunk: one batch call + up to 64
# individual retries = 65 * 120s ≈ 2.2 h (bounded; previously 600s each made a
# stuck batch + full retry pass unbounded in practice at ~10.9 h).
_TIMEOUT_SEC = 120


def exiftool_available() -> bool:
    return shutil.which("exiftool") is not None


def _run(files: Sequence[Path]) -> list[dict]:
    cmd = [
        "exiftool", "-json", "-XMP:all", "-charset", "filename=utf8", "--",
        *(str(f) for f in files),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=_TIMEOUT_SEC)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "exiftool failed").strip()[:500])
    return json.loads(proc.stdout or "[]")


def _flatten(tags: dict) -> dict:
    """Strip exiftool group prefixes ('XMP-dc:Title' -> 'Title')."""
    out: dict = {}
    for key, value in tags.items():
        if key in ("SourceFile",):
            continue
        out[key.split(":")[-1]] = value
    return out


def _match_record(
    records: list[dict], path: Path, chunk_basenames: list[str]
) -> dict | None:
    """Match an exiftool record to the requested file.

    Pass 1: exact SourceFile match over ALL records (authoritative).
    Pass 2 (fallback): basename match — only when that basename is UNIQUE
    within this chunk of files. With duplicate basenames in one batch,
    exiftool's record order is not a reliable mapping, so we return nothing
    rather than risk attributing tags to the wrong file.
    """
    wanted = str(path)
    for rec in records:
        if rec.get("SourceFile", "") == wanted:
            return rec
    base = Path(wanted).name
    if chunk_basenames.count(base) != 1:
        return None  # ambiguous within this batch -> refuse to guess
    for rec in records:
        if Path(rec.get("SourceFile", "")).name == base:
            return rec
    return None


def extract_xmp(paths: Sequence[Path]) -> dict[str, dict]:
    """Extract embedded XMP tags for many files.

    Returns {str(path): flattened_tag_dict}; files without XMP map to {}.
    If exiftool is missing, all values are {} and a warning is logged once.
    """
    result: dict[str, dict] = {str(p): {} for p in paths}
    paths = [p for p in paths]
    if not paths:
        return {}
    if not exiftool_available():
        log.warning("exiftool not found on PATH; XMP extraction skipped")
        return result

    for i in range(0, len(paths), BATCH_SIZE):
        chunk = [Path(p) for p in paths[i : i + BATCH_SIZE]]
        records: list[dict] = []
        try:
            records = _run(chunk)
        except Exception as exc:  # noqa: BLE001 - degrade per-file instead of failing scan
            log.warning("exiftool batch failed (%s); retrying individually", exc)
            for p in chunk:
                try:
                    records.extend(_run([p]))
                except Exception:  # noqa: BLE001
                    log.warning("exiftool failed for %s", p)
        chunk_basenames = [p.name for p in chunk]
        for p in chunk:
            rec = _match_record(records, p, chunk_basenames)
            if rec:
                result[str(p)] = _flatten(rec)
    return result
