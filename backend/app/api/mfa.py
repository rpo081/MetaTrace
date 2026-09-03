"""MFA (TOTP) endpoints: status, enroll, QR, confirm, verify, disable, regenerate."""
from __future__ import annotations

import io
import logging

import qrcode
from fastapi import APIRouter, Depends, HTTPException, Request, Response

from .. import db
from ..auth import (
    audit,
    create_access_token,
    create_refresh_token,
    verify_password,
)
from ..config import Settings
from ..dependencies import get_current_user, require_role
from ..models.auth import (
    MfaConfirmRequest,
    MfaDisableRequest,
    MfaRegenerateRequest,
    MfaVerifyRequest,
)
from ..mfa import (
    current_counter,
    decrypt_secret,
    encrypt_secret,
    generate_backup_codes,
    get_fernet,
    hash_backup_code,
    new_totp_secret,
    provisioning_url,
    verify_backup_code_hash,
    verify_totp,
)
from ..rate_limit import check_rate_limit as _check_rate_limit
from .auth import (
    _set_access_cookie,
    _set_refresh_cookie,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth/mfa", tags=["mfa"])

_mfa_roles = require_role("admin", "editor", "viewer")


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _fernet_or_500(settings: Settings) -> object:
    try:
        return get_fernet(settings.mfa_encryption_key)
    except ValueError:
        log.error("MFA endpoint called without METATRACE_MFA_ENCRYPTION_KEY configured")
        raise HTTPException(500, "MFA not configured") from None


def _client(request: Request) -> tuple[str | None, str | None]:
    return (
        request.client.host if request.client else None,
        request.headers.get("user-agent"),
    )


def _lockout_active(row) -> bool:
    if row["locked_until"] is None:
        return False
    from datetime import datetime, timezone

    locked_until = datetime.fromisoformat(row["locked_until"].replace("Z", "+00:00"))
    if locked_until.tzinfo is None:
        locked_until = locked_until.replace(tzinfo=timezone.utc)
    return locked_until > datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# GET /api/auth/mfa/status
# ---------------------------------------------------------------------------

@router.get("/status")
async def mfa_status(request: Request, response: Response, user=Depends(_mfa_roles)):
    _check_rate_limit(request, "30/minute", per_endpoint=True)
    settings = _settings(request)
    row = db.get_user_by_id(settings.db_path, user["id"])
    if row is None:
        raise HTTPException(401, "user not found")
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return {
        "enabled": bool(row["mfa_enabled"]),
        "enrolled_at": row["mfa_enrolled_at"],
        "backup_remaining": db.count_unused_mfa_backup_codes(settings.db_path, row["id"]),
        "has_pending": bool(row["mfa_secret"] and not row["mfa_enabled"]),
    }


# ---------------------------------------------------------------------------
# POST /api/auth/mfa/enroll
# ---------------------------------------------------------------------------

@router.post("/enroll")
async def mfa_enroll(request: Request, response: Response, user=Depends(_mfa_roles)):
    _check_rate_limit(request, "5/hour", per_endpoint=True)
    settings = _settings(request)
    fernet = _fernet_or_500(settings)
    db_path = settings.db_path

    row = db.get_user_by_id(db_path, user["id"])
    if row is None:
        raise HTTPException(401, "user not found")
    if row["mfa_enabled"]:
        raise HTTPException(400, "MFA already enabled")

    secret = new_totp_secret()
    db.set_mfa_secret(db_path, row["id"], encrypt_secret(fernet, secret))  # type: ignore[arg-type]

    ip, ua = _client(request)
    audit(db_path, user_id=row["id"], action="mfa_enroll", ip_address=ip, user_agent=ua)

    response.headers["Cache-Control"] = "no-store, max-age=0"
    return {
        "otpauth_url": provisioning_url(secret, row["username"]),
        "secret": secret,
    }


# ---------------------------------------------------------------------------
# GET /api/auth/mfa/qr — PNG bytes (avoids data: URI under strict CSP)
# ---------------------------------------------------------------------------

@router.get("/qr")
async def mfa_qr(request: Request, user=Depends(_mfa_roles)):
    _check_rate_limit(request, "30/minute", per_endpoint=True)
    settings = _settings(request)
    fernet = _fernet_or_500(settings)
    db_path = settings.db_path

    row = db.get_user_by_id(db_path, user["id"])
    if row is None:
        raise HTTPException(401, "user not found")
    if not row["mfa_secret"]:
        raise HTTPException(404, "no pending MFA enrollment")
    if row["mfa_enabled"]:
        raise HTTPException(400, "MFA already enabled")
    try:
        secret = decrypt_secret(fernet, row["mfa_secret"])  # type: ignore[arg-type]
    except ValueError:
        raise HTTPException(500, "MFA not configured") from None

    img = qrcode.make(provisioning_url(secret, row["username"]))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(
        content=buf.getvalue(),
        media_type="image/png",
        headers={"Cache-Control": "no-store, max-age=0"},
    )


# ---------------------------------------------------------------------------
# POST /api/auth/mfa/confirm
# ---------------------------------------------------------------------------

@router.post("/confirm")
async def mfa_confirm(
    request: Request, response: Response, body: MfaConfirmRequest, user=Depends(_mfa_roles)
):
    _check_rate_limit(request, "5/minute", per_endpoint=True)
    settings = _settings(request)
    fernet = _fernet_or_500(settings)
    db_path = settings.db_path

    row = db.get_user_by_id(db_path, user["id"])
    if row is None:
        raise HTTPException(401, "user not found")
    if row["mfa_enabled"]:
        raise HTTPException(400, "MFA already enabled")
    if not row["mfa_secret"]:
        raise HTTPException(400, "no pending MFA enrollment")
    try:
        secret = decrypt_secret(fernet, row["mfa_secret"])  # type: ignore[arg-type]
    except ValueError:
        raise HTTPException(500, "MFA not configured") from None

    if not verify_totp(secret, body.code):
        # Code-guessing here feeds the same per-account lockout as verify —
        # a hijacked session must not get unlimited confirm attempts.
        db.record_login_failure(
            db_path, row["id"], settings.max_failed_attempts, settings.lockout_duration_minutes
        )
        ip, ua = _client(request)
        audit(db_path, user_id=row["id"], action="mfa_verify_failed", ip_address=ip, user_agent=ua)
        raise HTTPException(401, "invalid code")

    db.enable_mfa(db_path, row["id"])
    # Seed the replay guard with this code's time-step so the enrollment
    # code cannot be reused for the first login in the same window.
    db.set_mfa_last_counter(db_path, row["id"], current_counter())
    codes = generate_backup_codes()
    db.store_mfa_backup_codes(db_path, row["id"], [hash_backup_code(c) for c in codes])

    ip, ua = _client(request)
    audit(db_path, user_id=row["id"], action="mfa_confirm", ip_address=ip, user_agent=ua)

    response.headers["Cache-Control"] = "no-store, max-age=0"
    return {"ok": True, "backup_codes": codes}


# ---------------------------------------------------------------------------
# Shared verify helper (TOTP path)
# ---------------------------------------------------------------------------

def _issue_session(request: Request, response: Response, settings: Settings, user_row) -> str:
    db_path = settings.db_path
    db.record_login_success(db_path, user_row["id"])
    access_token = create_access_token(
        user_row["id"], user_row["role"],
        secret=settings.jwt_secret,
        ttl_minutes=settings.access_token_ttl_minutes,
    )
    ip, ua = _client(request)
    refresh_raw, _meta = create_refresh_token(
        db_path, user_row["id"], ip=ip, ua=ua, ttl_days=settings.refresh_token_ttl_days,
    )
    _set_refresh_cookie(response, refresh_raw, settings, request)
    _set_access_cookie(response, access_token, settings, request)
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return access_token


def _verify_pre_auth_token(settings: Settings, mfa_token: str) -> dict:
    from ..mfa import decode_mfa_token

    if not settings.jwt_secret:
        raise HTTPException(500, "internal error")
    try:
        return decode_mfa_token(mfa_token, secret=settings.jwt_secret)
    except Exception:
        raise HTTPException(401, "invalid or expired code") from None


# ---------------------------------------------------------------------------
# POST /api/auth/mfa/verify
# ---------------------------------------------------------------------------

@router.post("/verify")
async def mfa_verify(request: Request, body: MfaVerifyRequest, response: Response):
    _check_rate_limit(request, "5/minute", per_endpoint=True)
    settings = _settings(request)
    fernet = _fernet_or_500(settings)
    db_path = settings.db_path

    payload = _verify_pre_auth_token(settings, body.mfa_token)
    user_id = int(payload["sub"])
    jti = payload["jti"]

    if db.is_mfa_token_jti_used(db_path, jti):
        raise HTTPException(401, "invalid or expired code")

    row = db.get_user_by_id(db_path, user_id)
    if row is None or not row["is_active"]:
        raise HTTPException(401, "invalid or expired code")
    if _lockout_active(row):
        raise HTTPException(423, "account temporarily locked")
    if not row["mfa_enabled"] or not row["mfa_secret"]:
        raise HTTPException(401, "invalid or expired code")

    try:
        secret = decrypt_secret(fernet, row["mfa_secret"])  # type: ignore[arg-type]
    except ValueError:
        raise HTTPException(500, "MFA not configured") from None

    if not verify_totp(secret, body.code):
        db.record_login_failure(
            db_path, row["id"], settings.max_failed_attempts, settings.lockout_duration_minutes
        )
        ip, ua = _client(request)
        audit(db_path, user_id=row["id"], action="mfa_verify_failed", ip_address=ip, user_agent=ua)
        raise HTTPException(401, "invalid or expired code")

    # Replay guard: one successful verify per 30 s time-step. The guarded
    # compare-and-set is atomic (SQLite serializes writers), so concurrent
    # verifies sharing one code collapse to a single winner even with
    # distinct pre-auth tokens — unlike the previous read-then-write.
    now_counter = current_counter()
    if not db.compare_and_set_mfa_counter(db_path, row["id"], now_counter):
        ip, ua = _client(request)
        audit(db_path, user_id=row["id"], action="mfa_verify_failed", ip_address=ip, user_agent=ua)
        raise HTTPException(401, "invalid or expired code")

    from datetime import datetime, timezone

    exp = datetime.fromtimestamp(payload["exp"], tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%fZ")
    if not db.consume_mfa_token_jti(db_path, jti, row["id"], exp):
        raise HTTPException(401, "invalid or expired code")

    fresh = db.get_user_by_id(db_path, row["id"])
    access_token = _issue_session(request, response, settings, fresh or row)

    ip, ua = _client(request)
    # Plain `login` row keeps SIEM/reporting keyed on action="login" complete
    # for MFA logins (research §2: `login` + `mfa_verified`).
    audit(db_path, user_id=row["id"], action="login", ip_address=ip, user_agent=ua)
    audit(db_path, user_id=row["id"], action="mfa_verified", ip_address=ip, user_agent=ua)

    return {"access_token": access_token, "token_type": "bearer"}


# ---------------------------------------------------------------------------
# POST /api/auth/mfa/verify-backup
# ---------------------------------------------------------------------------

@router.post("/verify-backup")
async def mfa_verify_backup(request: Request, body: MfaVerifyRequest, response: Response):
    _check_rate_limit(request, "5/minute", per_endpoint=True)
    settings = _settings(request)
    _fernet_or_500(settings)  # fail closed when MFA unconfigured
    db_path = settings.db_path

    payload = _verify_pre_auth_token(settings, body.mfa_token)
    user_id = int(payload["sub"])
    jti = payload["jti"]

    if db.is_mfa_token_jti_used(db_path, jti):
        raise HTTPException(401, "invalid or expired code")

    row = db.get_user_by_id(db_path, user_id)
    if row is None or not row["is_active"]:
        raise HTTPException(401, "invalid or expired code")
    if _lockout_active(row):
        raise HTTPException(423, "account temporarily locked")
    if not row["mfa_enabled"]:
        raise HTTPException(401, "invalid or expired code")

    candidates = db.list_mfa_backup_codes(db_path, row["id"])
    match = next(
        (c for c in candidates if c["used_at"] is None
         and verify_backup_code_hash(body.code, c["code_hash"])),
        None,
    )
    if match is None:
        db.record_login_failure(
            db_path, row["id"], settings.max_failed_attempts, settings.lockout_duration_minutes
        )
        ip, ua = _client(request)
        audit(db_path, user_id=row["id"], action="mfa_verify_failed", ip_address=ip, user_agent=ua)
        raise HTTPException(401, "invalid or expired code")

    from datetime import datetime, timezone

    # Consume the pre-auth token BEFORE burning the backup code: a raced or
    # expired JTI must not cost the user a single-use code.
    exp = datetime.fromtimestamp(payload["exp"], tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if not db.consume_mfa_token_jti(db_path, jti, row["id"], exp):
        raise HTTPException(401, "invalid or expired code")

    if db.mark_mfa_backup_code_used(db_path, match["code_hash"]) == 0:
        raise HTTPException(401, "invalid or expired code")

    fresh = db.get_user_by_id(db_path, row["id"])
    access_token = _issue_session(request, response, settings, fresh or row)

    ip, ua = _client(request)
    audit(db_path, user_id=row["id"], action="login", ip_address=ip, user_agent=ua)
    audit(db_path, user_id=row["id"], action="mfa_backup_used", ip_address=ip, user_agent=ua)

    return {"access_token": access_token, "token_type": "bearer"}


# ---------------------------------------------------------------------------
# POST /api/auth/mfa/disable
# ---------------------------------------------------------------------------

@router.post("/disable")
async def mfa_disable(
    request: Request, response: Response, body: MfaDisableRequest, user=Depends(get_current_user)
):
    _check_rate_limit(request, "10/minute", per_endpoint=True)
    settings = _settings(request)
    _fernet_or_500(settings)
    db_path = settings.db_path

    row = db.get_user_by_id(db_path, user["id"])
    if row is None:
        raise HTTPException(401, "user not found")
    if not verify_password(body.password, row["password_hash"]):
        # Same shared account lockout as login/confirm/verify.
        db.record_login_failure(
            db_path, row["id"], settings.max_failed_attempts, settings.lockout_duration_minutes
        )
        ip, ua = _client(request)
        audit(db_path, user_id=row["id"], action="mfa_verify_failed", ip_address=ip, user_agent=ua)
        raise HTTPException(400, "current password is incorrect")
    if row["mfa_enabled"]:
        if not body.code:
            raise HTTPException(400, "verification code required")
        if not row["mfa_secret"]:
            # Unreachable through normal flows (enabled implies a secret),
            # but fail controlled instead of AttributeError inside decrypt.
            raise HTTPException(500, "MFA not configured")
        fernet = get_fernet(settings.mfa_encryption_key)
        try:
            secret = decrypt_secret(fernet, row["mfa_secret"])  # type: ignore[arg-type]
        except ValueError:
            raise HTTPException(500, "MFA not configured") from None
        # Accept TOTP or an unused backup code as possession proof.
        totp_ok = verify_totp(secret, body.code)
        backup_match = next(
            (c for c in db.list_mfa_backup_codes(db_path, row["id"])
             if c["used_at"] is None and verify_backup_code_hash(body.code, c["code_hash"])),
            None,
        )
        if not (totp_ok or backup_match is not None):
            db.record_login_failure(
                db_path, row["id"], settings.max_failed_attempts, settings.lockout_duration_minutes
            )
            ip, ua = _client(request)
            audit(db_path, user_id=row["id"], action="mfa_verify_failed", ip_address=ip, user_agent=ua)
            raise HTTPException(401, "invalid code")
        if backup_match is not None:
            # Consume the proof explicitly (disable_mfa deletes all codes
            # anyway — this keeps the single-use invariant local to the branch).
            db.mark_mfa_backup_code_used(db_path, backup_match["code_hash"])

    db.disable_mfa(db_path, row["id"])
    ip, ua = _client(request)
    audit(db_path, user_id=row["id"], action="mfa_disabled", ip_address=ip, user_agent=ua)
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return {"ok": True}


# ---------------------------------------------------------------------------
# POST /api/auth/mfa/regenerate-codes
# ---------------------------------------------------------------------------

@router.post("/regenerate-codes")
async def mfa_regenerate_codes(
    request: Request, response: Response, body: MfaRegenerateRequest, user=Depends(_mfa_roles)
):
    _check_rate_limit(request, "5/minute", per_endpoint=True)
    settings = _settings(request)
    fernet = _fernet_or_500(settings)
    db_path = settings.db_path

    row = db.get_user_by_id(db_path, user["id"])
    if row is None:
        raise HTTPException(401, "user not found")
    if not row["mfa_enabled"] or not row["mfa_secret"]:
        raise HTTPException(400, "MFA not enabled")
    try:
        secret = decrypt_secret(fernet, row["mfa_secret"])  # type: ignore[arg-type]
    except ValueError:
        raise HTTPException(500, "MFA not configured") from None
    if not verify_totp(secret, body.code):
        db.record_login_failure(
            db_path, row["id"], settings.max_failed_attempts, settings.lockout_duration_minutes
        )
        ip, ua = _client(request)
        audit(db_path, user_id=row["id"], action="mfa_verify_failed", ip_address=ip, user_agent=ua)
        raise HTTPException(401, "invalid code")

    codes = generate_backup_codes()
    db.store_mfa_backup_codes(db_path, row["id"], [hash_backup_code(c) for c in codes])

    ip, ua = _client(request)
    audit(db_path, user_id=row["id"], action="mfa_codes_regenerated", ip_address=ip, user_agent=ua)
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return {"ok": True, "backup_codes": codes}
