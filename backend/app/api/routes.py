"""API routes."""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse
from PIL import Image, ImageOps, UnidentifiedImageError
from .. import db, embeddings, metadata, store_snapshot
from ..config import Settings
from ..dependencies import get_settings, require_role, require_role_with_query
from ..rate_limit import check_rate_limit as _check_rate_limit
from ..thumbs import prune_thumb_cache as _prune_thumb_cache
from ..thumbs import unlink_quiet as _unlink_quiet

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

# Chunk size for streamed upload reads (1 MiB) — bounds memory use and lets us
# abort oversized uploads before the whole body has been consumed.
UPLOAD_CHUNK_BYTES = 1024 * 1024


def _snapshot_scan_root_payload(request: Request) -> dict:
    s = request.app.state.settings
    configured = s.snapshot_scan_root
    default_root = str(s.default_snapshot_scan_root)
    return {
        "configured_root_path": str(configured) if configured is not None else None,
        "root_path": default_root,
        "default_root_path": default_root,
        "uses_default": configured is None,
        "source": "env" if configured is not None else "store_path",
    }


# Admin auth is now handled via FastAPI dependency injection:
#   dependencies=[Depends(require_role("admin"))]
# The legacy X-Admin-Token header is still supported via get_current_user_or_legacy_token.


def _sanitize_filename(name: str) -> str:
    """Sanitize filename for Content-Disposition: strip CR/LF, escape quotes."""
    # Remove CR/LF to block header injection, replace path separators and quotes
    sanitized = name.replace("\\", "_").replace("/", "_")
    sanitized = sanitized.replace('"', "_").replace("'", "_")
    sanitized = sanitized.replace("\r", "").replace("\n", "")
    # Strip control chars
    sanitized = "".join(c for c in sanitized if ord(c) >= 32)
    sanitized = sanitized.strip()
    return sanitized


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
    path = settings.latest_store_snapshot_file
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


def _file_size_mb(path: Path) -> float | None:
    try:
        return round(path.stat().st_size / (1024 * 1024), 2)
    except OSError:
        return None


def _snapshot_age_sec(settings) -> tuple[float | None, bool | None]:
    """Return (age_sec, is_stale) for the latest snapshot, or (None, None) if missing."""
    path = settings.latest_store_snapshot_file
    try:
        stat = path.stat()
    except OSError:
        return None, None
    age = time.time() - stat.st_mtime
    max_age_sec = settings.snapshot_max_age_hours * 3600 if settings.snapshot_max_age_hours else None
    is_stale = (max_age_sec is not None and age > max_age_sec) if max_age_sec else False
    return round(age, 1), is_stale


def _delta_age_sec(settings) -> float | None:
    path = settings.data_path / "rescan_delta_latest.json"
    try:
        return round(time.time() - path.stat().st_mtime, 1)
    except OSError:
        return None


def _thumbs_stats(settings) -> tuple[int, float | None]:
    try:
        thumbs_dir = settings.thumbs_dir
        if not thumbs_dir.is_dir():
            return 0, 0.0
        # Cached for 5s to avoid glob cost on 10s polling at 100k files
        cache = getattr(_thumbs_stats, "_cache", None)
        now = time.monotonic()
        if cache and now - cache[0] < 5.0:
            return cache[1]
        files = list(thumbs_dir.glob("*.png"))
        count = len(files)
        total = 0
        for p in files:
            try:
                total += p.stat().st_size
            except OSError:
                continue
        size_mb = round(total / (1024 * 1024), 2)
        _thumbs_stats._cache = (now, (count, size_mb))  # type: ignore[attr-defined]
        return count, size_mb
    except Exception:  # noqa: BLE001
        return 0, None


@router.get("/health")
def health() -> dict:
    return {"ok": True}


@router.get("/stats")
def stats(
    request: Request,
    response: Response,
    s: Settings = Depends(get_settings),
    user=Depends(require_role("admin", "editor", "viewer")),
) -> dict:
    _check_rate_limit(request, "30/minute")
    response.headers["Cache-Control"] = "private, no-store, max-age=0"
    st = request.app.state
    snap_age, snap_stale = _snapshot_age_sec(s)
    thumbs_count, thumbs_mb = _thumbs_stats(s)
    return {
        "indexed": st.indexer.count,
        "db_count": db.count(s.db_path),
        "snapshot_image_count": _snapshot_image_count(s, st),
        "snapshot_age_sec": snap_age,
        "snapshot_stale": snap_stale,
        "snapshot_max_age_hours": s.snapshot_max_age_hours,
        "delta_age_sec": _delta_age_sec(s),
        "index_file_size_mb": _file_size_mb(s.index_file),
        "db_file_size_mb": _file_size_mb(s.db_path),
        "thumbs_count": thumbs_count,
        "thumbs_size_mb": thumbs_mb,
        "thumbs_max_files": s.thumbs_max_files,
        "state": st.indexer.status["state"],
        "last_report": st.indexer.status["last_report"],
        "inventory_source": st.indexer.status.get("inventory_source"),
        "last_scan": db.kv_get(s.db_path, "last_scan"),
        "model": f"{s.model_name}:{s.model_pretrained}",
        "exiftool": metadata.exiftool_available(),
        "max_upload_mb": s.max_upload_mb,
    }


_VALID_SORT_COLUMNS = frozenset({
    "indexed_at", "mtime", "size", "rel_path", "width", "height", "id",
})

_VALID_ORDER = frozenset({"asc", "desc"})


@router.get("/images")
def browse_images(
    request: Request,
    response: Response,
    offset: int = Query(default=0, ge=0, le=200000),
    limit: int = Query(default=60, ge=1),
    sort: str = Query(default="indexed_at"),
    order: str = Query(default="desc"),
    cursor: str | None = Query(default=None, description="Opaque cursor for keyset pagination (indexed_at, id)"),
    size_min: int | None = Query(default=None, ge=0),
    size_max: int | None = Query(default=None, ge=0),
    width_min: int | None = Query(default=None, ge=0),
    width_max: int | None = Query(default=None, ge=0),
    height_min: int | None = Query(default=None, ge=0),
    height_max: int | None = Query(default=None, ge=0),
    indexed_from: str | None = Query(default=None),
    indexed_to: str | None = Query(default=None),
    mtime_from: float | None = Query(default=None),
    mtime_to: float | None = Query(default=None),
    ext: str | None = Query(default=None),
    folder: str | None = Query(default=None),
    q: str | None = Query(default=None),
    has_xmp: bool = Query(default=False),
    s: Settings = Depends(get_settings),
    user=Depends(require_role("admin", "editor", "viewer")),
) -> dict:
    _check_rate_limit(request, "30/minute")
    response.headers["Cache-Control"] = "private, no-store, max-age=0"
    if sort not in _VALID_SORT_COLUMNS:
        raise HTTPException(400, "invalid sort column")
    if order not in _VALID_ORDER:
        raise HTTPException(400, "invalid order (must be 'asc' or 'desc')")
    capped_limit = min(limit, s.max_browse_limit)
    # Guard for 200k scale: very high offsets without filters are expensive (SQLite OFFSET scan)
    if offset > 50000 and not any(v is not None for v in [size_min, size_max, width_min, width_max, height_min, height_max, ext, folder, q]) and not has_xmp and indexed_from is None and indexed_to is None and mtime_from is None and mtime_to is None:
        log.info("high offset browse without filters: offset=%d (may be slow at 200k)", offset)
    filters = {
        "size_min": size_min,
        "size_max": size_max,
        "width_min": width_min,
        "width_max": width_max,
        "height_min": height_min,
        "height_max": height_max,
        "indexed_from": indexed_from,
        "indexed_to": indexed_to,
        "mtime_from": mtime_from,
        "mtime_to": mtime_to,
        "ext": ext,
        "folder": folder,
        "q": q,
        "has_xmp": has_xmp,
    }
    # Strip None values so browse_images only sees active filters
    filters = {k: v for k, v in filters.items() if v is not None}

    total, rows = db.browse_images(
        s.db_path,
        offset=offset,
        limit=capped_limit,
        sort=sort,
        order=order,
        filters=filters,
        cursor=cursor,
    )
    items = []
    for row in rows:
        item = db.row_to_result(row)
        # DTO guard: attribute access
        item["size"] = getattr(row, "size", row["size"])
        item["mtime"] = getattr(row, "mtime", row["mtime"])
        item["sha256"] = getattr(row, "sha256", row["sha256"])
        item["indexed_at"] = getattr(row, "indexed_at", row["indexed_at"])
        rid = getattr(row, "id", row["id"])
        item["thumb_url"] = f"/api/thumb/{rid}"
        item["file_url"] = f"/api/file/{rid}"
        items.append(item)

    # Build next_cursor for cursor pagination (only meaningful when sort=indexed_at)
    next_cursor = None
    if rows and sort == "indexed_at":
        last = rows[-1]
        last_at = getattr(last, "indexed_at", last["indexed_at"])
        last_id = getattr(last, "id", last["id"])
        next_cursor = db._encode_cursor(last_at, int(last_id))

    # Keep backwards compat: offset pagination still works; when cursor is used,
    # has_more is based on whether we got a full page.
    if cursor is not None and sort == "indexed_at":
        has_more = len(rows) == capped_limit
    else:
        has_more = offset + capped_limit < total

    return {
        "items": items,
        "total": total,
        "offset": offset,
        "limit": capped_limit,
        "has_more": has_more,
        "next_cursor": next_cursor,
        "cursor": cursor,
    }


@router.get("/settings/store-snapshot")
def get_store_snapshot_settings(
    request: Request,
    s: Settings = Depends(get_settings),
    user=Depends(require_role("admin", "editor", "viewer")),
) -> dict:
    _check_rate_limit(request, "30/minute")
    return _snapshot_scan_root_payload(request)


@router.post("/settings/store-snapshot/run")
def run_store_snapshot(
    request: Request,
    s: Settings = Depends(get_settings),
    user=Depends(require_role("admin")),
) -> dict:
    effective = s.default_snapshot_scan_root
    try:
        log.info("starting store snapshot scan for %s", effective)
        result = store_snapshot.detect_changes(
            root_path=str(effective),
            snapshot_file=s.baseline_snapshot_file,
            data_folder=s.data_path,
            allowed_extensions=s.extensions,
            on_progress=lambda message: log.info("store snapshot: %s", message),
        )
    except ValueError as exc:
        log.warning("store snapshot scan failed for %s: %s", effective, exc)
        raise HTTPException(400, "invalid scan root") from None
    log.info(
        "finished store snapshot scan for %s in %.2fs (%d total changes)",
        result["root_path"],
        result["duration_sec"],
        result["summary"]["total_changes"],
    )
    return result


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
    response: Response,
    file: UploadFile | None = File(default=None),
    q: str | None = Query(default=None),
    combine: str = Query(default="and"),
    k: int | None = Query(default=None, ge=1),
    min_score: float = Query(default=None, ge=-1.0, le=1.0),
    s: Settings = Depends(get_settings),
    user=Depends(require_role("admin", "editor", "viewer")),
) -> dict:
    # Rate limit: 30 searches/minute per client IP
    _check_rate_limit(request, "30/minute")
    response.headers["Cache-Control"] = "private, no-store, max-age=0"

    st = request.app.state
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
def thumb(
    request: Request,
    image_id: int,
    size: int | None = Query(default=None),
    s: Settings = Depends(get_settings),
    user=Depends(require_role_with_query("admin", "editor", "viewer")),
) -> FileResponse:
    _check_rate_limit(request, "60/minute")
    st = request.app.state
    row = _get_row_or_404(request, image_id)
    side = min(max(size or s.thumb_size, 64), 1024)

    cache = s.thumbs_dir / f"{image_id}_{side}.png"
    if not cache.exists():
        rel = getattr(row, "rel_path", row["rel_path"])  # DTO attribute guard
        src = _store_file(s, rel)
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
                _prune_thumb_cache(s)
            except Exception:
                _unlink_quiet(tmp_name)
                raise
        except HTTPException:
            raise
        except Exception:  # noqa: BLE001
            log.exception("thumbnail generation failed for image %s", image_id)
            raise HTTPException(500, "thumbnail generation failed") from None
    return FileResponse(cache, media_type="image/png",
                        headers={"Cache-Control": "private, max-age=86400"})


@router.get("/file/{image_id}")
def original_file(
    request: Request,
    image_id: int,
    s: Settings = Depends(get_settings),
    user=Depends(require_role_with_query("admin", "editor", "viewer")),
) -> FileResponse:
    _check_rate_limit(request, "30/minute")
    row = _get_row_or_404(request, image_id)
    rel = getattr(row, "rel_path", row["rel_path"])  # DTO guard
    src = _store_file(s, rel)
    if not src.exists():
        raise HTTPException(404, "source file missing from store")
    # Inline display with sanitized filename; browser sniffing blocked by X-Content-Type-Options
    # RFC 5987: sanitize for header injection (CR/LF, quotes) and provide encoded fallback.
    from urllib.parse import quote as _quote
    raw_name = Path(rel).name
    safe_name = _sanitize_filename(raw_name) or f"image-{image_id}"
    quoted = _quote(safe_name, safe="")
    return FileResponse(
        src,
        headers={
            "Content-Disposition": f'inline; filename="{safe_name}"; filename*=UTF-8\'\'{quoted}',
            "Cache-Control": "private, max-age=86400",
        },
    )


@router.get("/rescan-delta")
def get_rescan_delta(
    request: Request,
    s: Settings = Depends(get_settings),
    user=Depends(require_role("admin", "editor", "viewer")),
) -> dict:
    """Return the latest delta JSON for optimized rescans.

    Returns:
        - status='ok': delta available with timestamp, summary, and changes
        - status='no_delta': no delta available (full rescan required)
    """
    _check_rate_limit(request, "30/minute")
    delta_file = s.data_path / "rescan_delta_latest.json"

    if not delta_file.exists():
        return {
            "status": "no_delta",
            "message": "No delta available",
        }

    try:
        validated = store_snapshot.load_and_validate_delta(delta_file)

        return {
            "status": "ok",
            "timestamp": validated.timestamp,
            "summary": validated.summary,
            "changes": validated.changes.model_dump(),
        }
    except Exception:  # noqa: BLE001
        log.exception("failed to read rescan delta")
        raise HTTPException(500, "failed to read rescan delta") from None


@router.post("/rescan", status_code=202)
def rescan(
    request: Request,
    rebuild: bool = Query(default=False),
    use_delta: bool = Query(default=True),
    s: Settings = Depends(get_settings),
    user=Depends(require_role("admin", "editor")),
) -> dict:
    """Start a rescan, optionally delta-optimized.

    Parameters:
        - rebuild: full rebuild of the index (ignores use_delta)
        - use_delta: when True and delta available, process only changes
    """
    # Rate limit: 5 rescans/minute per client IP
    _check_rate_limit(request, "5/minute")
    
    # Full rebuild is admin-only; editors may trigger delta rescans.
    if rebuild and user["role"] != "admin":
        raise HTTPException(403, "full rebuild requires admin role")
    
    # If rebuild=True, ignore delta
    if rebuild:
        ok = request.app.state.scheduler.trigger_now(rebuild=True)
        if not ok:
            raise HTTPException(409, "a scan is already running")
        return {"started": True, "rebuild": True, "delta_enabled": False}
    
    # Optional: pass validated delta info to scheduler (with staleness check)
    delta_info = None
    if use_delta:
        delta_file = s.data_path / "rescan_delta_latest.json"
        if delta_file.exists():
            try:
                validated = store_snapshot.load_and_validate_delta(delta_file)
                delta_info = {
                    "timestamp": validated.timestamp,
                    "summary": validated.summary,
                    "changes": validated.changes.model_dump(),
                }
                # Staleness guard: ignore delta older than 2x snapshot TTL (file mtime)
                max_age_hours = getattr(s, "snapshot_max_age_hours", 24) or 24
                delta_max_age_sec = max_age_hours * 3600 * 2
                try:
                    stat_age = time.time() - delta_file.stat().st_mtime
                    if stat_age > delta_max_age_sec:
                        log.warning(
                            "delta %s is stale (age=%.1fs > %dh); ignoring delta, falling back to full/diff",
                            delta_file.name, stat_age, max_age_hours * 2,
                        )
                        delta_info = None
                except OSError:
                    pass
            except Exception:  # noqa: BLE001
                log.warning("could not load delta, proceeding without delta")
                delta_info = None
    
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
def pause_rescan(
    request: Request,
    user=Depends(require_role("admin")),
) -> dict:
    _check_rate_limit(request, "10/minute")
    ok = request.app.state.scheduler.pause()
    if not ok:
        raise HTTPException(409, "no running scan to pause")
    return {"paused": True}


@router.post("/rescan/resume", status_code=202)
def resume_rescan(
    request: Request,
    user=Depends(require_role("admin")),
) -> dict:
    _check_rate_limit(request, "10/minute")
    ok = request.app.state.scheduler.resume()
    if not ok:
        raise HTTPException(409, "scan is not paused")
    return {"resumed": True}
