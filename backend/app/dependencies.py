"""FastAPI dependency chain for authentication and authorization."""
from __future__ import annotations

import logging
import secrets

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import db
from .auth import decode_access_token
from .config import Settings

log = logging.getLogger(__name__)

# We use HTTPBearer to extract the token from the Authorization header.
# auto_error=False lets us fall back to the legacy X-Admin-Token header.
_bearer_scheme = HTTPBearer(auto_error=False)


# ---------------------------------------------------------------------------
# Layer 1 – extract token from Authorization header
# ---------------------------------------------------------------------------

async def get_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> str:
    """Return the raw Bearer token or raise 401."""
    if credentials is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    return credentials.credentials


# Whitelist of endpoints a user with the `must_change_password` flag may call.
# All other authenticated endpoints must return 403 until the flag is cleared.
# The flag is cleared by `POST /api/auth/change-password`, so the user can
# always escape the gate by changing their password.
_PASSWORD_CHANGE_WHITELIST = frozenset({
    "/api/auth/change-password",
    "/api/auth/logout",
    "/api/auth/me",
})


def _enforce_password_change(request: Request, user_row) -> None:
    """Raise 403 if the user must change their password and the request path is
    not in the whitelist. Whitelisted paths (change-password, logout, me) are
    always permitted so the user can resolve the gate or abandon the session.

    Compares against integer literal 1 (not truthiness) so a future schema
    migration that flips the flag's representation — e.g. a bool column — is
    caught loudly here instead of silently treating every row as gated.
    """
    if user_row["must_change_password"] != 1:
        return
    if request.url.path in _PASSWORD_CHANGE_WHITELIST:
        return
    raise HTTPException(status_code=403, detail="password_change_required")


# ---------------------------------------------------------------------------
# Layer 2 – decode JWT, fetch user, verify active
# ---------------------------------------------------------------------------

def _decode_user_row(request: Request, token: str):
    """Decode a JWT and return the corresponding user row."""
    settings: Settings = request.app.state.settings
    try:
        payload = decode_access_token(token, secret=settings.jwt_secret)
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=401, detail="invalid or expired token") from None

    user_id = int(payload["sub"])
    row = db.get_user_by_id(settings.db_path, user_id)
    if row is None:
        raise HTTPException(status_code=401, detail="user not found")
    if not row["is_active"]:
        raise HTTPException(status_code=403, detail="account disabled")
    _enforce_password_change(request, row)
    return row


async def get_current_user(
    request: Request,
    token: str = Depends(get_token),
):
    """Decode JWT, fetch user, verify is_active.  Returns ``sqlite3.Row``."""
    return _decode_user_row(request, token)


# ---------------------------------------------------------------------------
# Layer 2b – JWT-or-legacy-token (transition helper)
# ---------------------------------------------------------------------------

async def get_current_user_or_legacy_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
):
    """Try JWT first; fall back to X-Admin-Token header; then trusted-LAN.

    This allows existing integrations that use the legacy header to keep
    working while new clients migrate to JWT auth.  When no admin token
    is configured the endpoint runs in trusted-LAN mode (unauthenticated
    access allowed) — preserving the pre-auth behaviour.
    """
    settings: Settings = request.app.state.settings

    # 1. Try Bearer JWT
    if credentials is not None:
        return _decode_user_row(request, credentials.credentials)

    # 2. Fall back to legacy X-Admin-Token header
    expected = settings.admin_token
    if expected:
        provided = request.headers.get("X-Admin-Token", "")
        if secrets.compare_digest(provided.encode("utf-8"), expected.encode("utf-8")):
            # Synthesise a virtual admin row so downstream code can treat it
            # uniformly.  We fabricate it as a dict (duck-typed like sqlite3.Row).
            return {
                "id": 0,
                "__virtual__": True,
                "username": "_legacy_admin",
                "email": "admin@legacy.local",
                "role": "admin",
                "is_active": True,
                "created_at": "",
                "last_login": None,
            }
        # Admin token is set but not provided or doesn't match
        raise HTTPException(status_code=403, detail="admin token required")

    # 3. Trusted-LAN mode: no admin token configured, no JWT — allow access
    #    only when the operator has explicitly opted in (M-2 security fix).
    if not settings.allow_unauthenticated:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=(
                "Authentication required. Set METATRACE_ADMIN_TOKEN or "
                "METATRACE_ALLOW_UNAUTH=true for trusted-LAN mode."
            ),
        )
    return {
        "id": 0,
        "__virtual__": True,
        "username": "_trusted_lan",
        "email": "trusted@lan.local",
        "role": "admin",
        "is_active": True,
        "created_at": "",
        "last_login": None,
    }


# ---------------------------------------------------------------------------
# Layer 2c – JWT-or-legacy-token with query param fallback (for image URLs)
# ---------------------------------------------------------------------------

async def get_current_user_or_legacy_token_with_query(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
):
    """Like ``get_current_user_or_legacy_token`` but also accepts token via query.

    Thumbnails and original files are loaded via ``<img src>`` which cannot send
    ``Authorization`` or ``X-Admin-Token`` headers.  When auth is required,
    the frontend appends ``?token=<JWT-or-admin-token>`` so the image request
    can still be authenticated.  Query token is treated with same precedence
    as the header fallback (JWT first, then legacy admin token).
    """
    settings: Settings = request.app.state.settings

    # 1. Try Bearer JWT from header
    if credentials is not None:
        return _decode_user_row(request, credentials.credentials)

    # 1b. Try JWT from query param ``token`` or ``access_token``
    qp_token = request.query_params.get("token") or request.query_params.get("access_token")
    if qp_token:
        try:
            return _decode_user_row(request, qp_token)
        except HTTPException:
            # Fall through to legacy check so an admin token in ``token`` also works
            pass

    # 2. Fall back to legacy X-Admin-Token header or query ``admin_token``/``token``
    expected = settings.admin_token
    if expected:
        provided = request.headers.get("X-Admin-Token", "")
        # Also accept admin token via query param for image URLs
        if not provided:
            provided = request.query_params.get("admin_token") or request.query_params.get("token") or ""
        # Legacy compare — catch non-ascii gracefully
        try:
            if secrets.compare_digest(provided.encode("utf-8"), expected.encode("utf-8")):
                return {
                    "id": 0,
                    "__virtual__": True,
                    "username": "_legacy_admin",
                    "email": "admin@legacy.local",
                    "role": "admin",
                    "is_active": True,
                    "created_at": "",
                    "last_login": None,
                }
        except Exception:
            pass
        raise HTTPException(status_code=403, detail="admin token required")

    # 3. Trusted-LAN mode
    if not settings.allow_unauthenticated:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=(
                "Authentication required. Set METATRACE_ADMIN_TOKEN or "
                "METATRACE_ALLOW_UNAUTH=true for trusted-LAN mode."
            ),
        )
    return {
        "id": 0,
        "__virtual__": True,
        "username": "_trusted_lan",
        "email": "trusted@lan.local",
        "role": "admin",
        "is_active": True,
        "created_at": "",
        "last_login": None,
    }


def require_role_with_query(*roles: str):
    """Like ``require_role`` but allows token via query param (for image GETs)."""
    async def _check(
        request: Request,
        user=Depends(get_current_user_or_legacy_token_with_query),
    ):
        user_role = user["role"] if isinstance(user, dict) else user["role"]
        if user_role not in roles:
            raise HTTPException(status_code=403, detail="insufficient permissions")
        return user
    return _check


# ---------------------------------------------------------------------------
# Layer 3 – role assertion (factory)
# ---------------------------------------------------------------------------

def require_role(*roles: str):
    """Return a dependency that asserts the authenticated user has one of *roles*.

    Usage::

        @router.post("/rescan", dependencies=[Depends(require_role("admin"))])
        def rescan(...): ...

    ``require_role("admin")`` works.

    During transition the dependency accepts the legacy X-Admin-Token header
    for the ``"admin"`` role via ``get_current_user_or_legacy_token``.
    """

    async def _check(
        request: Request,
        user=Depends(get_current_user_or_legacy_token),
    ):
        user_role = user["role"] if isinstance(user, dict) else user["role"]
        if user_role not in roles:
            raise HTTPException(status_code=403, detail="insufficient permissions")
        return user

    return _check
