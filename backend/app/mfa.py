"""TOTP two-factor authentication: Fernet encryption, pre-auth JWT, backup codes."""
from __future__ import annotations

import re
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone

import jwt
import pyotp
from cryptography.fernet import Fernet, InvalidToken

from .auth import _JWT_AUDIENCE, _JWT_ISSUER

MFA_PRE_AUTH_TTL_MINUTES = 5
MFA_TOTP_WINDOW = 1
MFA_BACKUP_CODE_COUNT = 10

_BACKUP_NORMALIZE_RE = re.compile(r"[^A-Z0-9]")


def get_fernet(mfa_key: str | None) -> Fernet:
    """Return a Fernet instance or raise ValueError when MFA is not configured."""
    if not mfa_key:
        raise ValueError("MFA not configured: set METATRACE_MFA_ENCRYPTION_KEY")
    try:
        return Fernet(mfa_key.encode())
    except Exception as exc:
        raise ValueError(f"invalid MFA encryption key: {exc}") from None


def encrypt_secret(fernet: Fernet, secret: str) -> str:
    return fernet.encrypt(secret.encode()).decode()


def decrypt_secret(fernet: Fernet, encrypted: str) -> str:
    try:
        return fernet.decrypt(encrypted.encode()).decode()
    except InvalidToken as exc:
        raise ValueError("cannot decrypt MFA secret") from exc


def new_totp_secret() -> str:
    return pyotp.random_base32()


def provisioning_url(secret: str, username: str, issuer: str = "MetaTrace") -> str:
    return pyotp.totp.TOTP(secret).provisioning_uri(name=username, issuer_name=issuer)


def verify_totp(secret: str, code: str) -> bool:
    """Verify a 6-digit TOTP code with ±1 step tolerance. No exceptions leak."""
    normalized = (code or "").strip().replace(" ", "")
    if not normalized.isdigit():
        return False
    try:
        return bool(pyotp.TOTP(secret).verify(normalized, valid_window=MFA_TOTP_WINDOW))
    except Exception:
        return False


def current_counter(timestamp: float | None = None) -> int:
    return int((timestamp if timestamp is not None else time.time()) // 30)


# ---------------------------------------------------------------------------
# Backup codes
# ---------------------------------------------------------------------------

def generate_backup_codes(count: int = MFA_BACKUP_CODE_COUNT) -> list[str]:
    """Generate ``XXXX-XXXX-XXXX`` codes (uppercase hex, 48 bits entropy each).

    48 bits + slow hashing keep offline brute-force of a stolen DB infeasible;
    the online path is additionally throttled (5/min) and lockout-guarded.
    """
    codes: list[str] = []
    for _ in range(count):
        raw = secrets.token_hex(6).upper()  # 12 hex chars
        codes.append(f"{raw[:4]}-{raw[4:8]}-{raw[8:]}")
    return codes


def normalize_backup_code(code: str) -> str:
    return _BACKUP_NORMALIZE_RE.sub("", (code or "").upper())


def hash_backup_code(code: str) -> str:
    """Argon2id-hash a backup code (per-code random salt included).

    Reuses the existing password-hashing primitive per research.md §3
    ("Argon2id ... or SHA-256 + per-code salt"). Verification via
    :func:`verify_backup_code_hash`; legacy SHA-256 rows simply never match.
    """
    from .auth import hash_password

    return hash_password(normalize_backup_code(code))


def verify_backup_code_hash(candidate: str, stored_hash: str) -> bool:
    from .auth import verify_password

    return verify_password(normalize_backup_code(candidate), stored_hash)


# ---------------------------------------------------------------------------
# Pre-auth (mfa_token) JWT — never accepted as an access token
# ---------------------------------------------------------------------------

def create_mfa_token(user_id: int, *, secret: str, ttl_minutes: int = MFA_PRE_AUTH_TTL_MINUTES) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "purpose": "mfa",
        "iss": _JWT_ISSUER,
        "aud": _JWT_AUDIENCE,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=ttl_minutes)).timestamp()),
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def decode_mfa_token(token: str, *, secret: str) -> dict:
    """Decode a pre-auth token. Raises ``jwt.InvalidTokenError`` on failure,
    ``ValueError`` when the purpose claim is not ``mfa``."""
    payload = jwt.decode(
        token,
        secret,
        algorithms=["HS256"],
        issuer=_JWT_ISSUER,
        audience=_JWT_AUDIENCE,
        options={"require": ["exp", "iat", "sub", "iss", "aud"]},
    )
    if payload.get("purpose") != "mfa":
        raise ValueError("not a pre-auth token")
    if not payload.get("jti"):
        raise ValueError("pre-auth token missing jti")
    return payload
