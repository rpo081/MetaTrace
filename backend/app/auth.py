"""Core authentication logic: password hashing, JWT, refresh tokens, audit."""
from __future__ import annotations

import hashlib
import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError, VerificationError
from argon2.low_level import Type

from . import db

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Password hashing (Argon2id – OWASP 2026 recommendations)
# ---------------------------------------------------------------------------

_ph = PasswordHasher(
    time_cost=2,
    memory_cost=19456,  # 19 MiB
    parallelism=1,
    hash_len=32,
    salt_len=16,
    type=Type.ID,
)


def hash_password(plain: str) -> str:
    """Return an Argon2id hash of *plain*."""
    return _ph.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """Timing-safe verify.  Returns ``False`` on any error."""
    try:
        return _ph.verify(hashed, plain)
    except (VerifyMismatchError, VerificationError, InvalidHashError) as exc:
        # Avoid spamming full exception string; type is enough for diagnostics
        log.debug("verify_password failed (%s)", type(exc).__name__)
        return False


# ---------------------------------------------------------------------------
# JWT access tokens
# ---------------------------------------------------------------------------

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


_JWT_ISSUER = "metatrace"
_JWT_AUDIENCE = "metatrace-api"


def create_access_token(user_id: int, role: str, *, secret: str, ttl_minutes: int = 15) -> str:
    """Issue a short-lived HS256 JWT.

    Rotation: changing METATRACE_JWT_SECRET invalidates all active access tokens
    (15m) and refresh flows will re-issue with the new secret on next refresh.
    Keep old secret briefly dual-validated during rolling deploy if zero-downtime rotation is needed.
    """
    now = _utcnow()
    payload = {
        "sub": str(user_id),
        "role": role,
        "iss": _JWT_ISSUER,
        "aud": _JWT_AUDIENCE,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=ttl_minutes)).timestamp()),
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def decode_access_token(token: str, *, secret: str) -> dict:
    """Decode and validate a JWT. Raises ``jwt.InvalidTokenError`` on failure."""
    return jwt.decode(
        token,
        secret,
        algorithms=["HS256"],
        issuer=_JWT_ISSUER,
        audience=_JWT_AUDIENCE,
        options={"require": ["exp", "iat", "sub", "iss", "aud"]},
    )


# ---------------------------------------------------------------------------
# HMAC signed URLs for file access (C-02: avoid JWT in query Referer)
# ---------------------------------------------------------------------------

def create_file_signature(secret: str, file_id: int, exp: int) -> str:
    """HMAC-SHA256 signature for ``file_id:exp`` using JWT secret as key."""
    import hashlib as _hashlib
    import hmac as _hmac

    msg = f"{file_id}:{exp}".encode()
    return _hmac.new(secret.encode(), msg, _hashlib.sha256).hexdigest()


def verify_file_signature(secret: str, file_id: int, exp: int, sig: str) -> bool:
    """Constant-time verify of file signature."""
    import hmac as _hmac

    expected = create_file_signature(secret, file_id, exp)
    return _hmac.compare_digest(expected, sig)


def build_signed_file_url(secret: str, file_id: int, ttl_sec: int = 300) -> str:
    """Return /api/file/{id}?sig=&exp= URL valid for ttl_sec."""
    import time as _time

    exp = int(_time.time()) + ttl_sec
    sig = create_file_signature(secret, file_id, exp)
    return f"/api/file/{file_id}?sig={sig}&exp={exp}"


# ---------------------------------------------------------------------------
# Refresh tokens (opaque, hashed before storage)
# ---------------------------------------------------------------------------

def _hash_token(raw: str) -> str:
    """SHA-256 of the raw token — we never store the plaintext."""
    return hashlib.sha256(raw.encode()).hexdigest()


def create_refresh_token(
    db_path: Path,
    user_id: int,
    *,
    ip: str | None = None,
    ua: str | None = None,
    ttl_days: int = 7,
) -> tuple[str, dict]:
    """Generate, store and return ``(raw_token, token_row_dict)``.

    The *token_row_dict* contains ``token_hash``, ``family_id``, ``expires_at``
    and is useful for callers that need metadata without a second DB hit.
    """
    raw = secrets.token_urlsafe(48)
    token_hash = _hash_token(raw)
    family_id = uuid.uuid4().hex
    expires_dt = _utcnow() + timedelta(days=ttl_days)
    expires_at = expires_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    db.store_refresh_token(
        db_path,
        user_id=user_id,
        token_hash=token_hash,
        family_id=family_id,
        expires_at=expires_at,
        ip_address=ip,
        user_agent=ua,
    )
    return raw, {
        "token_hash": token_hash,
        "family_id": family_id,
        "expires_at": expires_at,
    }


def rotate_refresh_token(
    db_path: Path,
    old_token: str,
    *,
    ip: str | None = None,
    ua: str | None = None,
    ttl_days: int = 7,
) -> tuple[str, dict]:
    """Validate *old_token*, detect reuse, issue a new token.

    Returns ``(new_raw_token, new_token_row_dict)``.

    Raises ``ValueError`` on invalid / expired / revoked / reused tokens.
    """
    old_hash = _hash_token(old_token)
    row = db.get_refresh_token_by_hash(db_path, old_hash)

    if row is None:
        raise ValueError("invalid refresh token")

    if row["revoked_at"] is not None:
        # Possible token reuse — revoke the entire family
        log.warning("refresh token reuse detected (family=%s, user=%s)",
                     row["family_id"], row["user_id"])
        db.revoke_token_family(db_path, row["family_id"])
        raise ValueError("refresh token has been revoked")

    if row["expires_at"] < _utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"):
        raise ValueError("refresh token expired")

    # Revoke the old token (rotation)
    db.revoke_refresh_token(db_path, old_hash)

    # Issue a new token in the same family
    raw = secrets.token_urlsafe(48)
    new_hash = _hash_token(raw)
    new_counter = row["rotation_counter"] + 1
    expires_dt = _utcnow() + timedelta(days=ttl_days)
    expires_at = expires_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    db.store_refresh_token(
        db_path,
        user_id=row["user_id"],
        token_hash=new_hash,
        family_id=row["family_id"],
        rotation_counter=new_counter,
        expires_at=expires_at,
        ip_address=ip,
        user_agent=ua,
    )
    return raw, {
        "token_hash": new_hash,
        "family_id": row["family_id"],
        "expires_at": expires_at,
    }


def revoke_refresh_token(db_path: Path, token: str) -> None:
    """Revoke a single refresh token."""
    db.revoke_refresh_token(db_path, _hash_token(token))


def revoke_all_user_tokens(db_path: Path, user_id: int) -> None:
    """Revoke every active refresh token for *user_id*."""
    db.revoke_all_user_tokens(db_path, user_id)


# ---------------------------------------------------------------------------
# Audit log helper
# ---------------------------------------------------------------------------

def audit(
    db_path: Path,
    *,
    user_id: int | None = None,
    action: str,
    resource: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    details: str | None = None,
) -> None:
    """Write a row to ``audit_log``.  Best-effort — failures are logged, never raised."""
    try:
        db.audit_log_insert(
            db_path,
            user_id=user_id,
            action=action,
            resource=resource,
            ip_address=ip_address,
            user_agent=user_agent,
            details=details,
        )
    except Exception:  # noqa: BLE001
        log.exception("failed to write audit log entry (action=%s)", action)
