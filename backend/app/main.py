"""FastAPI application factory."""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address

from . import db
from .api.routes import router
from .config import Settings
from .indexer import Indexer
from .scheduler import ScanScheduler
from .search import SearchService

log = logging.getLogger("metatrace")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _store_has_files(settings: Settings) -> bool:
    exts = settings.extensions
    for dirpath, dirnames, filenames in os.walk(settings.store_path):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if Path(name).suffix.lower() in exts:
                return True
    return False


# Thumbnail writes go through tempfile.mkstemp(dir=thumbs_dir, suffix=".tmp")
# and are atomically renamed; a crash can leave orphans behind. Covers both
# mkstemp's "tmpXXXXXXXX.tmp" shape and literal ".tmp*" names.
_THUMB_TMP_GLOBS = ("tmp*", "*.tmp", ".tmp*")


def _cleanup_orphan_thumb_temps(settings: Settings) -> int:
    """Best-effort startup sweep of orphaned temp files in the thumbs dir."""
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


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings.ensure_dirs()
        db.init_db(settings.db_path)

        orphan_temps = _cleanup_orphan_thumb_temps(settings)
        if orphan_temps:
            log.info("cleaned up %d orphaned thumbnail temp file(s)", orphan_temps)

        if not settings.admin_token:
            log.warning(
                "METATRACE_ADMIN_TOKEN is unset — trusted-LAN mode: mutating "
                "endpoints (POST /api/rescan) are unauthenticated. Set "
                "METATRACE_ADMIN_TOKEN to require X-Admin-Token."
            )

        indexer = Indexer(settings)
        indexer.load_or_create()
        app.state.settings = settings
        app.state.indexer = indexer
        app.state.search = SearchService(indexer, settings)

        scheduler = ScanScheduler(indexer)
        app.state.scheduler = scheduler
        if settings.run_initial_scan_on_start and indexer.count == 0 and _store_has_files(settings):
            log.info("empty index detected; starting initial scan of %s", settings.store_path)
            scheduler.trigger_now()
        log.info("MetaTrace ready (indexed=%d, model=%s:%s)",
                 indexer.count, settings.model_name, settings.model_pretrained)
        yield
        scheduler.stop()

    app = FastAPI(title="MetaTrace", version="0.1.0", lifespan=lifespan)

    # Rate limiting via slowapi — each app gets its own limiter instance
    # so rate-limit state doesn't leak between test fixtures.
    limiter = Limiter(key_func=get_remote_address)
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    # CORS is opt-in: the SPA is served same-origin, so no middleware is added
    # unless METATRACE_CORS_ORIGINS explicitly lists allowed origins.
    cors_origins = settings.cors_origin_list
    if cors_origins:
        log.info("CORS enabled for: %s", ", ".join(cors_origins))
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    else:
        log.info("CORS disabled (same-origin SPA); set METATRACE_CORS_ORIGINS to enable")

    app.add_middleware(SlowAPIMiddleware)

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        # style-src allows inline style attributes (score bars); scripts stay
        # locked to same-origin via default-src. img-src needs blob: for the
        # local query-image preview (URL.createObjectURL).
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' blob:; "
            "style-src 'self' 'unsafe-inline'; frame-ancestors 'none'",
        )
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
        response.headers.setdefault("X-Frame-Options", "DENY")
        return response

    app.include_router(router)

    static_dir = Path(__file__).resolve().parent.parent / "static"
    if static_dir.is_dir():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="ui")
    else:
        log.info("no bundled frontend at %s; API only", static_dir)
    return app


app = create_app()
