"""Pydantic request/response models for authentication."""
from __future__ import annotations

import re

from pydantic import BaseModel, EmailStr, Field, field_validator


# ---------------------------------------------------------------------------
# Shared response schemas
# ---------------------------------------------------------------------------

class UserPublic(BaseModel):
    id: int
    username: str
    email: str
    role: str
    is_active: bool
    created_at: str
    last_login: str | None


class UserListItem(BaseModel):
    id: int
    username: str
    email: str
    role: str
    is_active: bool
    created_at: str
    last_login: str | None


# ---------------------------------------------------------------------------
# Password complexity (P1 hardening — A-3)
# ---------------------------------------------------------------------------
# Length 8 per operator request; validator requires upper, lower, digit
# to block trivial passwords. Argon2id remains the real defence.

_PASSWORD_COMPLEXITY_MSG = (
    "password must be at least 8 characters and contain uppercase, lowercase and digit"
)
_UPPER_RE = re.compile(r"[A-Z]")
_LOWER_RE = re.compile(r"[a-z]")
_DIGIT_RE = re.compile(r"[0-9]")


def _validate_password_strength(value: str) -> str:
    if len(value) < 8:
        raise ValueError(_PASSWORD_COMPLEXITY_MSG)
    if not (_UPPER_RE.search(value) and _LOWER_RE.search(value) and _DIGIT_RE.search(value)):
        raise ValueError(_PASSWORD_COMPLEXITY_MSG)
    return value


# ---------------------------------------------------------------------------
# Auth endpoints
# ---------------------------------------------------------------------------

class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    email: EmailStr | None = Field(default=None)
    password: str = Field(..., min_length=8, max_length=128)
    role: str = Field(default="viewer", pattern=r"^(admin|editor|viewer)$")

    @field_validator("password")
    @classmethod
    def _password_strength(cls, v: str) -> str:
        return _validate_password_strength(v)


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserPublic


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(..., min_length=8, max_length=128)

    @field_validator("new_password")
    @classmethod
    def _password_strength(cls, v: str) -> str:
        return _validate_password_strength(v)


class MeResponse(BaseModel):
    user: UserPublic
    must_change_password: bool = False


# ---------------------------------------------------------------------------
# User management endpoints (admin CRUD)
# ---------------------------------------------------------------------------

class UserCreateRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    email: EmailStr | None = Field(default=None)
    password: str = Field(..., min_length=8, max_length=128)
    role: str = Field(default="viewer", pattern=r"^(admin|editor|viewer)$")

    @field_validator("password")
    @classmethod
    def _password_strength(cls, v: str) -> str:
        return _validate_password_strength(v)


class UserUpdateRequest(BaseModel):
    email: EmailStr | None = None
    role: str | None = Field(default=None, pattern=r"^(admin|editor|viewer)$")
    is_active: bool | None = None


class AdminResetPasswordRequest(BaseModel):
    new_password: str = Field(..., min_length=8, max_length=128)

    @field_validator("new_password")
    @classmethod
    def _password_strength(cls, v: str) -> str:
        return _validate_password_strength(v)
