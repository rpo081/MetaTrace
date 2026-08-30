"""Authentication endpoints: register, login, logout, refresh, me, change-password."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response
from limits import parse as parse_limit
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from slowapi.wrappers import Limit

from .. import db
from ..auth import (
    audit,
    create_access_token,
    create_refresh_token,
    hash_password,
    revoke_all_user_tokens,
    revoke_refresh_token,
    rotate_refresh_token,
    verify_password,
)
from ..config import Settings
from ..dependencies import get_current_user, require_role
from ..models.auth import (
    ChangePasswordRequest,
    LoginRequest,
    LoginResponse,
    MeResponse,
    RegisterRequest,
    UserPublic,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _check_rate_limit(request: Request, limit_str: str) -> None:
    limiter = request.app.state.limiter
    item = parse_limit(limit_str)
    # scope per endpoint to avoid cross-endpoint bucket sharing (e.g. login 10/min vs users 10/min)
    key = f"{get_remote_address(request)}:{request.url.path}"
    if not limiter._limiter.hit(item, key):
        limit_wrapper = Limit(
            limit=item,
            key_func=get_remote_address,
            scope=None,
            per_method=False,
            methods=None,
            error_message=None,
            exempt_when=None,
            cost=1,
            override_defaults=False,
        )
        request.state.view_rate_limit = (item, [key])
        raise RateLimitExceeded(limit_wrapper)


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _refresh_cookie_name() -> str:
    return "refresh_token"


REFRESH_COOKIE_PATH = "/api/auth/refresh"


def _set_refresh_cookie(response: Response, token: str, settings: Settings) -> None:
    response.set_cookie(
        key=_refresh_cookie_name(),
        value=token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_same_site,
        max_age=settings.refresh_token_ttl_days * 86400,
        path=REFRESH_COOKIE_PATH,
    )


def _clear_refresh_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        key=_refresh_cookie_name(),
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_same_site,
        path=REFRESH_COOKIE_PATH,
    )


def _user_to_public(row) -> UserPublic:
    return UserPublic(
        id=row["id"],
        username=row["username"],
        email=row["email"],
        role=row["role"],
        is_active=bool(row["is_active"]),
        created_at=row["created_at"],
        last_login=row["last_login"],
    )


# ---------------------------------------------------------------------------
# POST /api/auth/register  (admin-only)
# ---------------------------------------------------------------------------

@router.post("/register", status_code=201)
async def register(
    request: Request,
    body: RegisterRequest,
    admin=Depends(require_role("admin")),
):
    _check_rate_limit(request, "5/minute")
    settings = _settings(request)
    db_path = settings.db_path

    # Check for duplicates
    if db.get_user_by_username(db_path, body.username):
        raise HTTPException(409, "username already taken")
    if db.get_user_by_email(db_path, body.email):
        raise HTTPException(409, "email already registered")

    pw_hash = hash_password(body.password)
    user_id = db.create_user(
        db_path,
        username=body.username,
        email=body.email,
        password_hash=pw_hash,
        role=body.role,
    )

    audit(db_path, user_id=admin["id"], action="user_create",
          resource=f"user:{user_id}", ip_address=request.client.host if request.client else None,
          user_agent=request.headers.get("user-agent"), details=f"username={body.username}")

    return {"id": user_id, "username": body.username, "role": body.role}


# ---------------------------------------------------------------------------
# POST /api/auth/login
# ---------------------------------------------------------------------------

@router.post("/login", response_model=LoginResponse)
async def login(request: Request, body: LoginRequest, response: Response):
    _check_rate_limit(request, "10/minute")
    settings = _settings(request)
    db_path = settings.db_path

    row = db.get_user_by_username(db_path, body.username)
    if row is None:
        # Constant-time-ish: still hash so timing doesn't reveal user existence
        hash_password(body.password)
        raise HTTPException(401, "invalid credentials")

    # Account lockout check
    if row["locked_until"] is not None:
        from datetime import datetime, timezone
        locked_until = datetime.fromisoformat(row["locked_until"].replace("Z", "+00:00"))
        # SQLite datetime() returns naive UTC strings; attach tz if missing.
        if locked_until.tzinfo is None:
            locked_until = locked_until.replace(tzinfo=timezone.utc)
        if locked_until > datetime.now(timezone.utc):
            raise HTTPException(423, "account temporarily locked")
        else:
            # Lockout expired — reset failed attempts
            db.record_login_success(db_path, row["id"])

    if not verify_password(body.password, row["password_hash"]):
        db.record_login_failure(db_path, row["id"],
                                settings.max_failed_attempts,
                                settings.lockout_duration_minutes)
        audit(db_path, user_id=row["id"], action="login_failed",
              ip_address=request.client.host if request.client else None,
              user_agent=request.headers.get("user-agent"))
        raise HTTPException(401, "invalid credentials")

    if not row["is_active"]:
        raise HTTPException(403, "account disabled")

    # Success
    db.record_login_success(db_path, row["id"])
    access_token = create_access_token(
        row["id"], row["role"],
        secret=settings.jwt_secret,
        ttl_minutes=settings.access_token_ttl_minutes,
    )
    refresh_raw, _meta = create_refresh_token(
        db_path, row["id"],
        ip=request.client.host if request.client else None,
        ua=request.headers.get("user-agent"),
        ttl_days=settings.refresh_token_ttl_days,
    )

    _set_refresh_cookie(response, refresh_raw, settings)

    audit(db_path, user_id=row["id"], action="login",
          ip_address=request.client.host if request.client else None,
          user_agent=request.headers.get("user-agent"))

    return LoginResponse(
        access_token=access_token,
        user=_user_to_public(row),
    )


# ---------------------------------------------------------------------------
# POST /api/auth/logout
# ---------------------------------------------------------------------------

@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    refresh_token: str | None = Cookie(default=None, alias="refresh_token"),
    user=Depends(get_current_user),
):
    settings = _settings(request)
    db_path = settings.db_path

    if refresh_token:
        revoke_refresh_token(db_path, refresh_token)

    _clear_refresh_cookie(response, settings)

    audit(db_path, user_id=user["id"], action="logout",
          ip_address=request.client.host if request.client else None,
          user_agent=request.headers.get("user-agent"))

    return {"ok": True}


# ---------------------------------------------------------------------------
# POST /api/auth/refresh
# ---------------------------------------------------------------------------

@router.post("/refresh")
async def refresh(
    request: Request,
    response: Response,
    refresh_token: str | None = Cookie(default=None, alias="refresh_token"),
):
    _check_rate_limit(request, "5/minute")
    if not refresh_token:
        raise HTTPException(401, "no refresh token")

    settings = _settings(request)
    db_path = settings.db_path

    try:
        new_raw, _meta = rotate_refresh_token(
            db_path, refresh_token,
            ip=request.client.host if request.client else None,
            ua=request.headers.get("user-agent"),
            ttl_days=settings.refresh_token_ttl_days,
        )
    except ValueError as exc:
        raise HTTPException(401, str(exc)) from None

    # We need the user to issue a new access token
    token_hash = _meta["token_hash"]
    new_row = db.get_refresh_token_by_hash(db_path, token_hash)
    if new_row is None:
        raise HTTPException(500, "internal error") from None

    user_row = db.get_user_by_id(db_path, new_row["user_id"])
    if user_row is None or not user_row["is_active"]:
        raise HTTPException(401, "user not found or disabled")

    access_token = create_access_token(
        user_row["id"], user_row["role"],
        secret=settings.jwt_secret,
        ttl_minutes=settings.access_token_ttl_minutes,
    )

    _set_refresh_cookie(response, new_raw, settings)

    return {"access_token": access_token, "token_type": "bearer"}


# ---------------------------------------------------------------------------
# GET /api/auth/me
# ---------------------------------------------------------------------------

@router.get("/me", response_model=MeResponse)
async def me(request: Request, user=Depends(get_current_user)):
    settings = _settings(request)
    # Re-fetch the row to get the current must_change_password flag — the
    # `user` passed by the dependency is a sqlite3.Row that may have been
    # cached before the flag was set (e.g. flag flipped while a JWT was in
    # flight).
    fresh = db.get_user_by_id(settings.db_path, user["id"])
    return MeResponse(
        user=_user_to_public(fresh if fresh is not None else user),
        must_change_password=bool((fresh or user)["must_change_password"]),
    )


# ---------------------------------------------------------------------------
# POST /api/auth/change-password
# ---------------------------------------------------------------------------

@router.post("/change-password")
async def change_password(
    request: Request,
    body: ChangePasswordRequest,
    user=Depends(get_current_user),
):
    _check_rate_limit(request, "10/minute")
    settings = _settings(request)
    db_path = settings.db_path

    # Re-fetch the user to get the current hash (the Depends user is already fetched,
    # but let's be explicit about getting the latest hash).
    user_row = db.get_user_by_id(db_path, user["id"])
    if user_row is None:
        raise HTTPException(401, "user not found")

    if not verify_password(body.current_password, user_row["password_hash"]):
        raise HTTPException(400, "current password is incorrect")

    new_hash = hash_password(body.new_password)
    db.update_user(db_path, user["id"], password_hash=new_hash)

    # Clear the must-change-password flag if it was set — successful
    # change-password is the only path that satisfies the server-side gate.
    db.clear_must_change_password(db_path, user["id"])

    # Revoke all existing refresh tokens (force re-login everywhere)
    revoke_all_user_tokens(db_path, user["id"])

    audit(db_path, user_id=user["id"], action="password_change",
          ip_address=request.client.host if request.client else None,
          user_agent=request.headers.get("user-agent"))

    return {"ok": True}
