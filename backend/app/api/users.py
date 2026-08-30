"""Admin-only user management endpoints."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from limits import parse as parse_limit
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from slowapi.wrappers import Limit

from .. import db
from ..auth import audit, hash_password, revoke_all_user_tokens
from ..dependencies import require_role
from ..models.auth import (
    AdminResetPasswordRequest,
    UserCreateRequest,
    UserListItem,
    UserUpdateRequest,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/users", tags=["users"])

# All endpoints in this router require admin role.
_admin = Depends(require_role("admin"))


def _check_rate_limit(request: Request, limit_str: str) -> None:
    """Mirror the helper in ``routes.py`` so user-management endpoints are
    rate-limited per client IP. Keeps the dependency local to avoid coupling
    this router to a private function in another module."""
    limiter = request.app.state.limiter
    item = parse_limit(limit_str)
    key = get_remote_address(request)
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


def _serialize_user(row) -> UserListItem:
    return UserListItem(
        id=row["id"],
        username=row["username"],
        email=row["email"],
        role=row["role"],
        is_active=bool(row["is_active"]),
        created_at=row["created_at"],
        last_login=row["last_login"],
    )


# ---------------------------------------------------------------------------
# GET /api/users — list all users
# ---------------------------------------------------------------------------

@router.get("", dependencies=[_admin])
async def list_users(request: Request):
    _check_rate_limit(request, "30/minute")
    rows = db.list_users(request.app.state.settings.db_path)
    return {"users": [_serialize_user(r) for r in rows]}


# ---------------------------------------------------------------------------
# POST /api/users — create user
# ---------------------------------------------------------------------------

@router.post("", status_code=201, dependencies=[_admin])
async def create_user(
    request: Request,
    body: UserCreateRequest,
    admin=Depends(require_role("admin")),
):
    _check_rate_limit(request, "10/minute")
    settings = request.app.state.settings
    db_path = settings.db_path

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
          resource=f"user:{user_id}",
          ip_address=request.client.host if request.client else None,
          user_agent=request.headers.get("user-agent"),
          details=f"username={body.username}, role={body.role}")

    row = db.get_user_by_id(db_path, user_id)
    return _serialize_user(row)


# ---------------------------------------------------------------------------
# GET /api/users/{id} — get user details
# ---------------------------------------------------------------------------

@router.get("/{user_id}", dependencies=[_admin])
async def get_user(request: Request, user_id: int):
    _check_rate_limit(request, "30/minute")
    row = db.get_user_by_id(request.app.state.settings.db_path, user_id)
    if row is None:
        raise HTTPException(404, "user not found")
    return _serialize_user(row)


# ---------------------------------------------------------------------------
# PATCH /api/users/{id} — update user
# ---------------------------------------------------------------------------

@router.patch("/{user_id}", dependencies=[_admin])
async def update_user(request: Request, user_id: int, body: UserUpdateRequest):
    _check_rate_limit(request, "10/minute")
    settings = request.app.state.settings
    db_path = settings.db_path

    existing = db.get_user_by_id(db_path, user_id)
    if existing is None:
        raise HTTPException(404, "user not found")

    # Check email uniqueness if changing
    if body.email is not None and body.email != existing["email"]:
        if db.get_user_by_email(db_path, body.email):
            raise HTTPException(409, "email already registered")

    # Prevent demoting/disabling the last active admin (mirrors DELETE guard)
    if existing["role"] == "admin" and existing["is_active"]:
        will_demote = body.role is not None and body.role != "admin"
        will_disable = body.is_active is False
        if will_demote or will_disable:
            rows = db.list_users(db_path)
            admin_count = sum(1 for r in rows if r["role"] == "admin" and r["is_active"])
            # current user is active admin, so count includes it
            if admin_count <= 1:
                raise HTTPException(400, "cannot demote or disable the last admin user")

    updated = db.update_user(
        db_path,
        user_id,
        email=body.email,
        role=body.role,
        is_active=int(body.is_active) if body.is_active is not None else None,
    )

    if not updated:
        raise HTTPException(400, "no fields to update")

    # If disabling, revoke all tokens
    if body.is_active is False:
        revoke_all_user_tokens(db_path, user_id)

    audit(db_path, action="user_update", resource=f"user:{user_id}",
          details=str(body.model_dump(exclude_unset=True)))

    row = db.get_user_by_id(db_path, user_id)
    return _serialize_user(row)


# ---------------------------------------------------------------------------
# DELETE /api/users/{id} — delete user
# ---------------------------------------------------------------------------

@router.delete("/{user_id}", status_code=204, dependencies=[_admin])
async def delete_user(request: Request, user_id: int):
    _check_rate_limit(request, "10/minute")
    settings = request.app.state.settings
    db_path = settings.db_path

    existing = db.get_user_by_id(db_path, user_id)
    if existing is None:
        raise HTTPException(404, "user not found")

    # Prevent deleting the last admin
    if existing["role"] == "admin":
        rows = db.list_users(db_path)
        admin_count = sum(1 for r in rows if r["role"] == "admin")
        if admin_count <= 1:
            raise HTTPException(400, "cannot delete the last admin user")

    revoke_all_user_tokens(db_path, user_id)
    db.delete_user(db_path, user_id)

    audit(db_path, action="user_delete", resource=f"user:{user_id}",
          details=f"username={existing['username']}")


# ---------------------------------------------------------------------------
# POST /api/users/{id}/reset-password — admin reset
# ---------------------------------------------------------------------------

@router.post("/{user_id}/reset-password", dependencies=[_admin])
async def reset_password(request: Request, user_id: int, body: AdminResetPasswordRequest):
    _check_rate_limit(request, "10/minute")
    settings = request.app.state.settings
    db_path = settings.db_path

    existing = db.get_user_by_id(db_path, user_id)
    if existing is None:
        raise HTTPException(404, "user not found")

    pw_hash = hash_password(body.new_password)
    db.update_user(db_path, user_id, password_hash=pw_hash)

    # Revoke all refresh tokens so the user must re-login
    revoke_all_user_tokens(db_path, user_id)

    audit(db_path, action="admin_reset_password", resource=f"user:{user_id}",
          details=f"username={existing['username']}")

    return {"ok": True}
