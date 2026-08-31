"""FastAPI application factory."""
from __future__ import annotations

import logging
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware


class _TokenRedactFilter(logging.Filter):
    """Redact `token=` query param from log records to avoid JWT leakage (A2).

    Query-token auth (`?token=`) is kept for `<img>` fallback, but must never
    appear in access logs, proxy logs, or Referer. This filter scrubs it at
    the logging layer as defense-in-depth; the preferred path is `fetch+blob`
    with `Authorization` header (`AuthenticatedImage`).
    """

    _RE = re.compile(r"([?;&](?:token|sig|exp)=)[^&\s\"']+")

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
            if "token=" in msg:
                # Scrub token value in the formatted message
                scrubbed = self._RE.sub(r"\1***", msg)
                # Mutate args so subsequent formatting is also scrubbed
                if record.args:
                    # For %-style args, args may contain URL; scrub there too
                    new_args = []
                    for a in record.args if isinstance(record.args, tuple) else [record.args]:
                        if isinstance(a, str) and "token=" in a:
                            new_args.append(self._RE.sub(r"\1***", a))
                        else:
                            new_args.append(a)
                    record.args = tuple(new_args) if isinstance(record.args, tuple) else new_args[0] if new_args else record.args
                record.msg = scrubbed
                record.args = ()
        except Exception:
            pass
        return True

from . import db
from .api.routes import router
from .api.auth import router as auth_router
from .api.users import router as users_router
from .auth import audit, hash_password
from .config import Settings
from .indexer import Indexer
from .scheduler import ScanScheduler
from .search import SearchService
from .thumbs import cleanup_orphan_thumb_temps, prune_thumb_cache

log = logging.getLogger("metatrace")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

# Install token-redacting filter on all handlers (uvicorn access logs included).
_redact_filter = _TokenRedactFilter()
for _h in logging.getLogger().handlers:
    _h.addFilter(_redact_filter)
logging.getLogger("uvicorn.access").addFilter(_redact_filter)
logging.getLogger("uvicorn.error").addFilter(_redact_filter)


def _store_has_files(settings: Settings) -> bool:
    exts = settings.extensions
    for dirpath, dirnames, filenames in os.walk(settings.store_path):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if Path(name).suffix.lower() in exts:
                return True
    return False


DEFAULT_ADMIN_PASSWORD = "changeme"


def _seed_admin_if_needed(settings: Settings) -> None:
    """Create an admin user when the users table is empty.

    Bootstrap policy (see plan-frontend §1.3):

    - If ``settings.admin_token`` is set, use it as the seed password.
      Existing short-token warnings remain so operators notice low entropy.
    - Otherwise, fall back to the literal ``DEFAULT_ADMIN_PASSWORD`` so the
      service is usable out-of-the-box without env config. The seeded user
      is marked ``must_change_password = 1`` so the operator is forced
      to set a real password on first login (server-side enforced; see
      ``dependencies._enforce_password_change``).
    """
    if db.user_count(settings.db_path) > 0:
        return

    token = settings.admin_token
    if not token:
        # SECURITY: the seed password is intentionally NOT interpolated into
        # this log line. Plaintext credentials in stdout/container logs/syslog
        # would defeat the first-login must-change guarantee — anyone with
        # log-read access could authenticate as the well-known `admin` user.
        log.info(
            "no users and no METATRACE_ADMIN_TOKEN — seeding default admin user; "
            "operator must change the password on first login."
        )
        seed_password = DEFAULT_ADMIN_PASSWORD
        seed_source = "default_seed"
    else:
        if len(token) < 12:
            log.warning(
                "METATRACE_ADMIN_TOKEN is very short (%d chars) — seeded admin password "
                "inherits low entropy; use `python -c \"import secrets; print(secrets.token_urlsafe(32))\"` "
                "and change the admin password after first login.", len(token)
            )
        elif len(token) < 32:
            log.warning(
                "METATRACE_ADMIN_TOKEN is %d chars; recommended >=32 chars "
                "(e.g. `secrets.token_urlsafe(32)`) for seeded admin password entropy.", len(token)
            )
        seed_password = token
        seed_source = "from_token"

    # Use the admin token as the username convention.
    username = "admin"
    email = "admin@metatrace.local"

    try:
        # NOTE: bypasses `_validate_password_strength` because DEFAULT_ADMIN_PASSWORD
        # ("changeme") does not meet the operator-enforced complexity rules. The
        # server-side `must_change_password` flag forces a real password on first
        # login, so this is a one-shot seed, not a permissive bypass.
        pw_hash = hash_password(seed_password)
        user_id = db.create_user(
            settings.db_path,
            username=username,
            email=email,
            password_hash=pw_hash,
            role="admin",
            must_change_password=True,
        )
        audit(settings.db_path, user_id=user_id, action="seed_admin",
              details=seed_source)
        log.warning(
            "seeded admin user (id=%d, source=%s) — change the password "
            "immediately via /api/auth/change-password (forced on first login).",
            user_id, seed_source,
        )
    except Exception:  # noqa: BLE001
        log.exception("failed to seed admin user")





def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings.ensure_dirs()
        db.init_db(settings.db_path)

        orphan_temps = cleanup_orphan_thumb_temps(settings)
        if orphan_temps:
            log.info("cleaned up %d orphaned thumbnail temp file(s)", orphan_temps)
        pruned = prune_thumb_cache(settings)
        if pruned:
            log.info("pruned %d excess thumbnail(s) on startup", pruned)

        # Seed an admin user from METATRACE_ADMIN_TOKEN if no users exist yet.
        _seed_admin_if_needed(settings)

        if not settings.admin_token:
            log.warning(
                "METATRACE_ADMIN_TOKEN is unset — mutating endpoints are open "
                "(trusted-LAN mode). Set METATRACE_ADMIN_TOKEN to require authentication."
            )

        if not settings.jwt_secret and not settings.allow_unauthenticated:
            # A-4 hardening: internet-facing deployments must set JWT secret.
            # Keep warning (not crash) to preserve legacy X-Admin-Token mode,
            # but log at error level so it stands out.
            log.error(
                "METATRACE_JWT_SECRET is unset and METATRACE_ALLOW_UNAUTH is false — "
                "JWT auth (login/refresh) will fail; set a 32+ char secret (secrets.token_urlsafe(32)) "
                "or set METATRACE_ALLOW_UNAUTH=true only for trusted-LAN."
            )

        # Short-token warning now handled centrally in _seed_admin_if_needed;
        # keep a startup note for existing DBs where admin already seeded.
        if settings.admin_token and len(settings.admin_token) < 12:
            log.warning(
                "METATRACE_ADMIN_TOKEN is very short — seeded admin password inherits its low "
                "entropy; set a longer token or change the admin password after first login."
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
    # Use X-Forwarded-For when METATRACE_TRUSTED_PROXY=true (behind nginx/traefik).
    from .rate_limit import _get_client_ip

    limiter = Limiter(key_func=_get_client_ip)
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
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "X-Admin-Token"],
        )
    else:
        log.info("CORS disabled (same-origin SPA); set METATRACE_CORS_ORIGINS to enable")

    app.add_middleware(SlowAPIMiddleware)

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        # CSP: no unsafe-inline — inline styles removed (score bars use JS, icons use class).
        # script-src locked to self mitigates XSS token exfiltration (D-1).
        # img-src needs blob: for local query-image preview (URL.createObjectURL).
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; img-src 'self' blob:; "
            "style-src 'self'; frame-ancestors 'none'",
        )
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        # HSTS only over HTTPS per spec — cookie_secure alone is insufficient
        if request.url.scheme == "https":
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response

    app.include_router(router)
    app.include_router(auth_router)
    app.include_router(users_router)

    static_dir = Path(__file__).resolve().parent.parent / "static"
    if static_dir.is_dir():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="ui")
    else:
        log.info("no bundled frontend at %s; API only", static_dir)
    return app


app = create_app()
