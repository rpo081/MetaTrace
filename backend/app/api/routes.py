"""API routes."""
from __future__ import annotations

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
        "state": st.indexer.status["state"],
        "last_report": st.indexer.status["last_report"],
        "last_scan": db.kv_get(s.db_path, "last_scan"),
        "model": f"{s.model_name}:{s.model_pretrained}",
        "rescan_interval_min": s.rescan_interval_min,
        "exiftool": metadata.exiftool_available(),
        "max_upload_mb": s.max_upload_mb,
    }


async def _read_upload(file: UploadFile, max_bytes: int) -> bytes:
    """Read an upload in chunks, aborting as soon as the limit is exceeded."""
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
        raise HTTPException(400, "empty upload")
    return data


@router.post("/search")
async def search(
    request: Request,
    file: UploadFile = File(...),
    k: int | None = Query(default=None, ge=1),
    min_score: float = Query(default=None, ge=-1.0, le=1.0),
) -> dict:
    st = request.app.state
    s = st.settings
    k = k or s.default_top_k
    k = min(k, s.max_top_k)
    if min_score is None:
        min_score = s.min_score_default

    data = await _read_upload(file, s.max_upload_bytes)

    try:
        return st.search.search(data, k, min_score)
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


@router.post("/rescan", status_code=202)
def rescan(request: Request, rebuild: bool = Query(default=False)) -> dict:
    _require_admin_token(request)
    ok = request.app.state.scheduler.trigger_now(rebuild=rebuild)
    if not ok:
        raise HTTPException(409, "a scan is already running")
    return {"started": True, "rebuild": rebuild}
