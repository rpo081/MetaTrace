"""API routes."""
from __future__ import annotations

import json
import logging
import os
import secrets
import tempfile
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from PIL import Image, ImageOps, UnidentifiedImageError

from .. import db, embeddings, metadata

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

# Chunk size for streamed upload reads (1 MiB) — bounds memory use and lets us
# abort oversized uploads before the whole body has been consumed.
UPLOAD_CHUNK_BYTES = 1024 * 1024

_warned_trusted_lan = False


def _require_admin_token(request: Request) -> None:
    """Guard mutating endpoints with METATRACE_ADMIN_TOKEN when configured.

    Token set  -> header X-Admin-Token must match (timing-safe compare), else 403.
    Token unset -> trusted-LAN mode: allowed, warn once at first use.
    """
    global _warned_trusted_lan
    expected = request.app.state.settings.admin_token
    if not expected:
        if not _warned_trusted_lan:
            log.warning(
                "METATRACE_ADMIN_TOKEN is not set; mutating endpoints are open "
                "(trusted-LAN mode). Set METATRACE_ADMIN_TOKEN to require "
                "X-Admin-Token on POST /api/rescan."
            )
            _warned_trusted_lan = True
        return
    provided = request.headers.get("X-Admin-Token", "")
    # Compare bytes: compare_digest raises TypeError on non-ASCII str, which
    # would turn a malformed header into a 500 instead of a 403.
    if not secrets.compare_digest(
        provided.encode("utf-8"), expected.encode("utf-8")
    ):
        raise HTTPException(403, "admin token required")


def _unlink_quiet(path: str) -> None:
    """Best-effort cleanup of a failed temp file."""
    try:
        os.unlink(path)
    except OSError:
        pass


def _store_file(settings, rel_path: str) -> Path:
    """Resolve a store-relative path and enforce containment in the store root.

    Raises HTTPException(400) when the resolved path escapes the store
    (traversal or symlink escape). Returns the fully resolved path.
    """
    root = settings.store_path.resolve()
    src = (root / rel_path).resolve()
    if not src.is_relative_to(root):
        log.warning("blocked store path escape: rel_path=%r", rel_path)
        raise HTTPException(400, "invalid path")
    return src


def _snapshot_image_count(settings, state) -> int | None:
    path = settings.store_snapshot_file
    try:
        stat = path.stat()
    except OSError:
        return None

    cache = getattr(state, "_snapshot_image_count_cache", None)
    fingerprint = (stat.st_mtime_ns, stat.st_size)
    if cache and cache[0] == fingerprint:
        return cache[1]

    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not read store snapshot image count: %s", exc)
        return cache[1] if cache else None

    entries = payload.get("files") if isinstance(payload, dict) and "files" in payload else payload
    if not isinstance(entries, dict):
        log.warning("ignoring invalid store snapshot payload in %s", path.name)
        return cache[1] if cache else None

    count = sum(1 for rel_path in entries if Path(rel_path).suffix.lower() in settings.extensions)
    state._snapshot_image_count_cache = (fingerprint, count)
    return count


@router.get("/health")
def health() -> dict:
    return {"ok": True}


@router.get("/stats")
def stats(request: Request) -> dict:
    st = request.app.state
    s = st.settings
    return {
        "indexed": st.indexer.count,
        "db_count": db.count(s.db_path),
        "snapshot_image_count": _snapshot_image_count(s, st),
        "state": st.indexer.status["state"],
        "last_report": st.indexer.status["last_report"],
        "inventory_source": st.indexer.status.get("inventory_source"),
        "last_scan": db.kv_get(s.db_path, "last_scan"),
        "model": f"{s.model_name}:{s.model_pretrained}",
        "exiftool": metadata.exiftool_available(),
        "max_upload_mb": s.max_upload_mb,
    }


async def _read_upload(file: UploadFile | None, max_bytes: int) -> bytes | None:
    """Read an upload in chunks, aborting as soon as the limit is exceeded."""
    if file is None or not file.filename:
        return None
    chunks: list[bytes] = []
    total = 0
    while chunk := await file.read(UPLOAD_CHUNK_BYTES):
        total += len(chunk)
        if total > max_bytes:
            mb = max_bytes // (1024 * 1024)
            raise HTTPException(413, f"upload exceeds {mb} MiB limit")
        chunks.append(chunk)
    data = b"".join(chunks)
    if not data:
        return None
    return data


@router.post("/search")
async def search(
    request: Request,
    file: UploadFile | None = File(default=None),
    q: str | None = Query(default=None),
    combine: str = Query(default="and"),
    k: int | None = Query(default=None, ge=1),
    min_score: float = Query(default=None, ge=-1.0, le=1.0),
) -> dict:
    st = request.app.state
    s = st.settings
    k = k or s.default_top_k
    k = min(k, s.max_top_k)
    if min_score is None:
        min_score = s.min_score_default
    if combine.lower() not in {"and", "or"}:
        raise HTTPException(400, "combine must be 'and' or 'or'")

    data = await _read_upload(file, s.max_upload_bytes)
    if data is None and (not q or not q.strip()):
        raise HTTPException(400, "Provide an image file or a text search query")

    try:
        return st.search.search(data, k, min_score, q, combine)
    except (ValueError, UnidentifiedImageError):
        log.warning("search upload could not be decoded", exc_info=True)
        raise HTTPException(400, "could not decode image") from None


def _get_row_or_404(request: Request, image_id: int):
    row = db.get_by_id(request.app.state.settings.db_path, image_id)
    if row is None:
        raise HTTPException(404, "unknown image id")
    return row


@router.get("/thumb/{image_id}")
def thumb(request: Request, image_id: int, size: int | None = Query(default=None)) -> FileResponse:
    st = request.app.state
    s = st.settings
    row = _get_row_or_404(request, image_id)
    side = min(max(size or s.thumb_size, 64), 1024)

    cache = s.thumbs_dir / f"{image_id}_{side}.png"
    if not cache.exists():
        src = _store_file(s, row["rel_path"])
        if not src.exists():
            raise HTTPException(404, "source file missing from store")
        try:
            try:
                with Image.open(src) as original:
                    img = ImageOps.exif_transpose(original)
                    if "A" in img.getbands():
                        img = img.convert("RGBA")
                    else:
                        img = img.convert("RGB")
            except UnidentifiedImageError:
                # PSDs Pillow cannot open still use the existing psd-tools fallback.
                img = embeddings.decode_image(src)
            img.thumbnail((side, side))
            # Write to a unique temp name next to the cache, then atomically
            # replace so concurrent requests never serve a partial PNG.
            fd, tmp_name = tempfile.mkstemp(dir=s.thumbs_dir, suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as fh:
                    img.save(fh, "PNG", optimize=True)
                os.replace(tmp_name, cache)
            except BaseException:
                _unlink_quiet(tmp_name)
                raise
        except HTTPException:
            raise
        except Exception:  # noqa: BLE001
            log.exception("thumbnail generation failed for image %s", image_id)
            raise HTTPException(500, "thumbnail generation failed") from None
    return FileResponse(cache, media_type="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})


@router.get("/file/{image_id}")
def original_file(request: Request, image_id: int) -> FileResponse:
    s = request.app.state.settings
    row = _get_row_or_404(request, image_id)
    src = _store_file(s, row["rel_path"])
    if not src.exists():
        raise HTTPException(404, "source file missing from store")
    return FileResponse(src)


@router.get("/rescan-delta")
def get_rescan_delta(request: Request) -> dict:
    """Gibt das neueste Delta JSON für optimierte Rescans zurück.
    
    Returns:
        - status='ok': Delta verfügbar mit timestamp, summary, und changes
        - status='no_delta': Kein Delta verfügbar (Vollständiger Rescan nötig)
    """
    s = request.app.state.settings
    delta_file = s.data_path / "rescan_delta_latest.json"
    
    if not delta_file.exists():
        return {
            "status": "no_delta",
            "message": "Kein Delta verfügbar",
        }
    
    try:
        with open(delta_file, "r") as f:
            delta_data = json.load(f)
        
        return {
            "status": "ok",
            "timestamp": delta_data["timestamp"],
            "summary": delta_data["summary"],
            "changes": delta_data["changes"]
        }
    except Exception as e:  # noqa: BLE001
        log.exception("failed to read rescan delta")
        raise HTTPException(500, "failed to read rescan delta") from None


@router.post("/rescan", status_code=202)
def rescan(request: Request, rebuild: bool = Query(default=False), use_delta: bool = Query(default=True)) -> dict:
    """Startet einen Rescan, optional mit Delta-Optimierung.
    
    Parameters:
        - rebuild: Vollständiger Rebuild des Index (ignoriert use_delta)
        - use_delta: Wenn True und Delta verfügbar, verarbeite nur Änderungen
    """
    _require_admin_token(request)
    
    # Wenn rebuild=True, ignoriere Delta
    if rebuild:
        ok = request.app.state.scheduler.trigger_now(rebuild=True)
        if not ok:
            raise HTTPException(409, "a scan is already running")
        return {"started": True, "rebuild": True, "delta_enabled": False}
    
    # Optional: Delta-Informationen an Scheduler übergeben
    delta_info = None
    if use_delta:
        s = request.app.state.settings
        delta_file = s.data_path / "rescan_delta_latest.json"
        if delta_file.exists():
            try:
                with open(delta_file, "r") as f:
                    delta_info = json.load(f)
            except Exception:  # noqa: BLE001
                log.warning("could not load delta, proceeding without delta")
    
    ok = request.app.state.scheduler.trigger_now(rebuild=False, delta_info=delta_info)
    if not ok:
        raise HTTPException(409, "a scan is already running")
    
    has_delta = delta_info is not None
    return {
        "started": True,
        "rebuild": False,
        "delta_enabled": use_delta,
        "has_delta": has_delta,
        "delta_summary": delta_info["summary"] if has_delta else None
    }


@router.post("/rescan/pause", status_code=202)
def pause_rescan(request: Request) -> dict:
    _require_admin_token(request)
    ok = request.app.state.scheduler.pause()
    if not ok:
        raise HTTPException(409, "no running scan to pause")
    return {"paused": True}


@router.post("/rescan/resume", status_code=202)
def resume_rescan(request: Request) -> dict:
    _require_admin_token(request)
    ok = request.app.state.scheduler.resume()
    if not ok:
        raise HTTPException(409, "scan is not paused")
    return {"resumed": True}
