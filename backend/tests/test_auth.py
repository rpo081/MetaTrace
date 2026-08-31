"""Comprehensive tests for the JWT + httpOnly cookie authentication system."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from backend.app import db, embeddings, metadata
from backend.app.auth import (
    _hash_token,
    create_access_token,
    create_refresh_token,
    decode_access_token,
    hash_password,
    revoke_all_user_tokens,
    revoke_refresh_token,
    rotate_refresh_token,
    verify_password,
)
from backend.app.config import Settings
from backend.app.indexer import Indexer
from backend.app.main import create_app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _embed_fake(images, settings):
    return np.ones((len(images), 8), dtype=np.float32)


@pytest.fixture()
def app(tmp_path, monkeypatch):
    """Yield (app, settings) with lifespan run — no admin_token (trusted-LAN)."""
    monkeypatch.setattr(embeddings, "embed_images", _embed_fake)
    monkeypatch.setattr(metadata, "extract_xmp", lambda paths: {})
    store = tmp_path / "store"
    store.mkdir()
    Image.new("RGB", (8, 8), (1, 2, 3)).save(store / "x.png")
    settings = Settings(
        store_path=store,
        data_path=tmp_path / "data",
        run_initial_scan_on_start=False,
        batch_size=8,
        jwt_secret="test-jwt-secret-32-chars-minimum-length!!",
        admin_token=None,
        allow_unauthenticated=True,
    )
    Indexer(settings).incremental(trigger="seed")
    _app = create_app(settings)
    yield _app, settings


@pytest.fixture()
def client(app):
    with TestClient(app[0]) as c:
        yield c


@pytest.fixture()
def client_localhost_port(app):
    """TestClient whose request URL carries an explicit ``http://localhost:8000``
    authority (default TestClient is ``http://testserver`` with no port). Lets
    the F3 port-mismatch case hit the explicit-port comparison instead of the
    hostname-mismatch path."""
    with TestClient(app[0], base_url="http://localhost:8000") as c:
        yield c


@pytest.fixture()
def client_cors(tmp_path, monkeypatch):
    """App for the split-origin SPA deployment: ``METATRACE_CORS_ORIGINS``
    allows ``http://localhost:5173`` while the API request URL is a different
    host (``testserver``). Exercises the NEW-1 CORS-aware origin guard."""
    monkeypatch.setattr(embeddings, "embed_images", _embed_fake)
    monkeypatch.setattr(metadata, "extract_xmp", lambda paths: {})
    store = tmp_path / "store"
    store.mkdir()
    Image.new("RGB", (8, 8), (1, 2, 3)).save(store / "x.png")
    settings = Settings(
        store_path=store,
        data_path=tmp_path / "data",
        run_initial_scan_on_start=False,
        batch_size=8,
        jwt_secret="test-jwt-secret-32-chars-minimum-length!!",
        admin_token=None,
        allow_unauthenticated=True,
        cors_origins="http://localhost:5173",
    )
    Indexer(settings).incremental(trigger="seed")
    with TestClient(create_app(settings)) as c:
        yield c


@pytest.fixture()
def settings(app):
    return app[1]


@pytest.fixture()
def app_admin(tmp_path, monkeypatch):
    """App with admin_token set — Bearer auth only (no trusted-LAN)."""
    monkeypatch.setattr(embeddings, "embed_images", _embed_fake)
    monkeypatch.setattr(metadata, "extract_xmp", lambda paths: {})
    store = tmp_path / "store"
    store.mkdir()
    Image.new("RGB", (8, 8), (1, 2, 3)).save(store / "x.png")
    settings = Settings(
        store_path=store,
        data_path=tmp_path / "data",
        run_initial_scan_on_start=False,
        batch_size=8,
        jwt_secret="test-jwt-secret-32-chars-minimum-length!!",
        admin_token="admintoken1234567890123456789012",
    )
    Indexer(settings).incremental(trigger="seed")
    _app = create_app(settings)
    with TestClient(_app) as c:
        yield c, settings


def _register_admin_token(client, token, **kwargs):
    """Register via X-Admin-Token header (the legacy path for admin_token mode)."""
    defaults = {
        "username": "testuser", "email": "test@example.com",
        "password": "Secure-Password-123", "role": "viewer",
    }
    defaults.update(kwargs)
    return client.post("/api/auth/register", json=defaults,
                       headers={"X-Admin-Token": token})


def _register_trusted_lan(client, **kwargs):
    """Register in trusted-LAN mode (no auth needed)."""
    defaults = {
        "username": "testuser", "email": "test@example.com",
        "password": "Secure-Password-123", "role": "viewer",
    }
    defaults.update(kwargs)
    return client.post("/api/auth/register", json=defaults)


def _login(client, username="testuser", password="Secure-Password-123"):
    return client.post("/api/auth/login", json={
        "username": username, "password": password,
    })


def _get_tokens(client, username="testuser", password="Secure-Password-123"):
    r = _login(client, username, password)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    data = r.json()
    access_token = data["access_token"]
    refresh_token = None
    for cookie in client.cookies.jar:
        if cookie.name == "refresh_token":
            refresh_token = cookie.value
    return access_token, refresh_token


def _auth_headers(access_token):
    return {"Authorization": f"Bearer {access_token}"}


def _get_refresh_cookie(client):
    for cookie in client.cookies.jar:
        if cookie.name == "refresh_token":
            return cookie.value
    return None


# ===========================================================================
# PASSWORD HASHING TESTS
# ===========================================================================

class TestPasswordHashing:
    def test_hash_and_verify(self):
        pw = "my-secure-password-123"
        h = hash_password(pw)
        assert h != pw
        assert verify_password(pw, h) is True

    def test_verify_wrong_password(self):
        h = hash_password("correct-password")
        assert verify_password("wrong-password", h) is False

    def test_verify_returns_false_on_corrupt_hash(self):
        """Corrupt hash must not crash; it should return False."""
        result = verify_password("anything", "not-a-valid-hash")
        assert result is False

    def test_hashes_are_unique(self):
        h1 = hash_password("same-password")
        h2 = hash_password("same-password")
        assert h1 != h2


# ===========================================================================
# JWT ACCESS TOKEN TESTS
# ===========================================================================

class TestAccessToken:
    def test_create_and_decode(self):
        secret = "a-secret-that-is-long-enough-for-hmac!!!"
        token = create_access_token(42, "admin", secret=secret, ttl_minutes=15)
        payload = decode_access_token(token, secret=secret)
        assert payload["sub"] == "42"
        assert payload["role"] == "admin"
        assert "exp" in payload
        assert "iat" in payload
        assert "jti" in payload

    def test_decode_with_wrong_secret(self):
        secret = "a-secret-that-is-long-enough-for-hmac!!!"
        token = create_access_token(1, "viewer", secret=secret)
        with pytest.raises(Exception):
            decode_access_token(token, secret="wrong-secret-aaaaaaaaaaaaaaaaaaaaaaa")

    def test_decode_expired_token(self):
        secret = "a-secret-that-is-long-enough-for-hmac!!!"
        token = create_access_token(1, "viewer", secret=secret, ttl_minutes=-1)
        with pytest.raises(Exception):
            decode_access_token(token, secret=secret)


# ===========================================================================
# REGISTER ENDPOINT TESTS
# ===========================================================================

class TestRegister:
    def test_register_requires_admin_token(self, app_admin):
        """With admin_token set, register requires admin auth."""
        c, _ = app_admin
        r = c.post("/api/auth/register", json={
            "username": "testuser", "email": "test@example.com",
            "password": "Secure-Password-123", "role": "viewer",
        })
        assert r.status_code in (401, 403)

    def test_register_trusted_lan(self, client):
        """Without admin_token, trusted-LAN allows registration."""
        r = _register_trusted_lan(client)
        assert r.status_code == 201
        body = r.json()
        assert body["username"] == "testuser"
        assert body["role"] == "viewer"

    def test_register_with_admin_token_header(self, app_admin):
        """Register using X-Admin-Token header."""
        c, _ = app_admin
        r = _register_admin_token(c, "admintoken1234567890123456789012")
        assert r.status_code == 201

    def test_register_duplicate_username(self, client):
        _register_trusted_lan(client, username="dup", email="a@example.com")
        r = _register_trusted_lan(client, username="dup", email="b@example.com")
        assert r.status_code == 409
        assert "username" in r.json()["detail"]

    def test_register_duplicate_email(self, client):
        _register_trusted_lan(client, username="user1", email="same@example.com")
        r = _register_trusted_lan(client, username="user2", email="same@example.com")
        assert r.status_code == 409
        assert "email" in r.json()["detail"]

    def test_register_invalid_email(self, client):
        r = client.post("/api/auth/register", json={
            "username": "testuser", "email": "not-an-email",
            "password": "Secure-Password-123", "role": "viewer",
        })
        assert r.status_code == 422

    def test_register_short_password(self, client):
        r = client.post("/api/auth/register", json={
            "username": "testuser", "email": "test@example.com",
            "password": "short", "role": "viewer",
        })
        assert r.status_code == 422

    def test_register_weak_password_rejected(self, client):
        """L-1 hardening: password must contain uppercase + digit."""
        for weak in ("alllowercase1", "ALLUPPERCASE", "NoDigitsHere", "short1A"):
            r = client.post("/api/auth/register", json={
                "username": "testuser", "email": "test@example.com",
                "password": weak, "role": "viewer",
            })
            assert r.status_code == 422, f"weak password {weak!r} was accepted"

    def test_register_invalid_role(self, client):
        r = client.post("/api/auth/register", json={
            "username": "testuser", "email": "test@example.com",
            "password": "Secure-Password-123", "role": "superadmin",
        })
        assert r.status_code == 422

    def test_register_invalid_username_chars(self, client):
        r = client.post("/api/auth/register", json={
            "username": "user name!", "email": "test@example.com",
            "password": "Secure-Password-123", "role": "viewer",
        })
        assert r.status_code == 422


# ===========================================================================
# LOGIN ENDPOINT TESTS
# ===========================================================================

class TestLogin:
    def test_login_success(self, client):
        _register_trusted_lan(client)
        r = _login(client)
        assert r.status_code == 200
        body = r.json()
        assert "access_token" in body
        assert body["token_type"] == "bearer"
        assert body["user"]["username"] == "testuser"
        assert body["user"]["role"] == "viewer"

    def test_login_sets_refresh_cookie(self, client):
        _register_trusted_lan(client)
        r = _login(client)
        assert r.status_code == 200
        set_cookie = r.headers.get("set-cookie", "")
        assert "refresh_token=" in set_cookie
        assert "httponly" in set_cookie.lower()
        assert "samesite" in set_cookie.lower()

    def test_login_wrong_password(self, client):
        _register_trusted_lan(client)
        r = _login(client, password="wrong-password")
        assert r.status_code == 401

    def test_login_nonexistent_user(self, client):
        r = _login(client, username="nobody")
        assert r.status_code == 401

    def test_login_disabled_account(self, app_admin):
        """After disabling an account, login returns 403."""
        c, settings = app_admin
        _register_admin_token(c, "admintoken1234567890123456789012", username="disablme", email="d@test.com")
        at, _ = _get_tokens(c, "disablme")
        # Get our user id
        me = c.get("/api/auth/me", headers=_auth_headers(at))
        uid = me.json()["user"]["id"]
        # Disable via admin API (X-Admin-Token)
        r_disable = c.patch(f"/api/users/{uid}", json={"is_active": False},
                            headers={"X-Admin-Token": "admintoken1234567890123456789012"})
        assert r_disable.status_code == 200
        # Login should fail with 403
        r = _login(c, username="disablme")
        assert r.status_code == 403
        assert "disabled" in r.json()["detail"]

    def test_login_lockout_after_failures(self, app_admin):
        """After max_failed_attempts, account is locked."""
        c, settings = app_admin
        _register_admin_token(c, "admintoken1234567890123456789012",
                              username="lockme", email="lock@test.com",
                              password="Good-Password-123")
        for i in range(5):
            r = _login(c, username="lockme", password=f"bad-{i}")
            assert r.status_code == 401
        # 6th attempt should be locked
        r = _login(c, username="lockme", password="Good-Password-123")
        assert r.status_code == 423
        assert "locked" in r.json()["detail"]


# ===========================================================================
# ME ENDPOINT TESTS
# ===========================================================================

class TestMe:
    def test_me_with_valid_token(self, client):
        _register_trusted_lan(client)
        at, _ = _get_tokens(client)
        r = client.get("/api/auth/me", headers=_auth_headers(at))
        assert r.status_code == 200
        user = r.json()["user"]
        assert user["username"] == "testuser"
        assert user["email"] == "test@example.com"

    def test_me_without_token(self, client):
        r = client.get("/api/auth/me")
        assert r.status_code == 401

    def test_me_with_invalid_token(self, client):
        r = client.get("/api/auth/me", headers={"Authorization": "Bearer invalid.token.here"})
        assert r.status_code == 401

    def test_me_with_expired_token(self, client, settings):
        _register_trusted_lan(client)
        expired = create_access_token(1, "viewer", secret=settings.jwt_secret, ttl_minutes=-1)
        r = client.get("/api/auth/me", headers=_auth_headers(expired))
        assert r.status_code == 401


# ===========================================================================
# REFRESH TOKEN TESTS
# ===========================================================================

class TestRefresh:
    def test_refresh_success(self, client):
        _register_trusted_lan(client)
        at, rt = _get_tokens(client)
        assert rt is not None
        r = client.post("/api/auth/refresh", cookies={"refresh_token": rt})
        assert r.status_code == 200
        body = r.json()
        assert "access_token" in body
        assert body["token_type"] == "bearer"

    def test_refresh_rotates_token(self, client):
        _register_trusted_lan(client)
        _, rt1 = _get_tokens(client)
        r = client.post("/api/auth/refresh", cookies={"refresh_token": rt1})
        assert r.status_code == 200
        rt2 = _get_refresh_cookie(client)
        assert rt2 is not None
        assert rt1 != rt2

    def test_refresh_old_token_invalid_after_rotation(self, client):
        _register_trusted_lan(client)
        _, rt1 = _get_tokens(client)
        client.post("/api/auth/refresh", cookies={"refresh_token": rt1})
        r = client.post("/api/auth/refresh", cookies={"refresh_token": rt1})
        assert r.status_code == 401

    def test_refresh_without_cookie(self, client):
        r = client.post("/api/auth/refresh")
        assert r.status_code == 401

    def test_refresh_revoked_token_detects_reuse(self, client, settings):
        _register_trusted_lan(client)
        _, rt1 = _get_tokens(client)
        rt1_hash = _hash_token(rt1)
        db.revoke_refresh_token(settings.db_path, rt1_hash)
        r = client.post("/api/auth/refresh", cookies={"refresh_token": rt1})
        assert r.status_code == 401

    # ------------------------------------------------------------------
    # F1 — atomic refresh-token rotation (concurrent replay = reuse)
    # ------------------------------------------------------------------

    def test_revoke_refresh_token_returns_rowcount(self, client, settings):
        """F1: db.revoke_refresh_token returns the number of rows updated so
        the rotation path can detect a concurrent revoke (0 = already gone)."""
        _register_trusted_lan(client)
        _, rt = _get_tokens(client)
        rt_hash = _hash_token(rt)
        assert db.revoke_refresh_token(settings.db_path, rt_hash) == 1
        assert db.revoke_refresh_token(settings.db_path, rt_hash) == 0

    def test_rotate_refresh_token_concurrent_rotation_is_reuse(self, client, settings, monkeypatch):
        """F1: if the revoke UPDATE matched 0 rows (another request rotated
        this exact token between our SELECT and UPDATE), rotate_refresh_token
        must kill the whole family and raise — no second live token minted."""
        _register_trusted_lan(client)
        _, rt = _get_tokens(client)
        # Simulate the interleaving: the concurrent request already revoked the
        # token, so our guarded UPDATE matches 0 rows.
        monkeypatch.setattr(db, "revoke_refresh_token", lambda path, token_hash: 0)
        with pytest.raises(ValueError, match="refresh token has been revoked"):
            rotate_refresh_token(settings.db_path, rt)
        # Family revocation must have marked the token revoked in the DB.
        row = db.get_refresh_token_by_hash(settings.db_path, _hash_token(rt))
        assert row is not None and row["revoked_at"] is not None

    # ------------------------------------------------------------------
    # F2 — generic 401 detail on /api/auth/refresh (no token-state leak)
    # ------------------------------------------------------------------

    def test_refresh_all_401_paths_share_generic_detail(self, client, settings):
        """F2: missing cookie, revoked token (reuse) and disabled user all
        produce the same generic 401 detail."""
        _register_trusted_lan(client)
        _, rt = _get_tokens(client)

        # (1) missing cookie
        r_no_cookie = client.post("/api/auth/refresh")
        assert r_no_cookie.status_code == 401
        d_no_cookie = r_no_cookie.json()["detail"]

        # (2) revoked token -> reuse detection path
        db.revoke_refresh_token(settings.db_path, _hash_token(rt))
        r_revoked = client.post("/api/auth/refresh", cookies={"refresh_token": rt})
        assert r_revoked.status_code == 401
        d_revoked = r_revoked.json()["detail"]

        # (3) refresh succeeds rotation-wise but the user is disabled
        _register_trusted_lan(client, username="disabled", email="dis@test.com")
        _, rt2 = _get_tokens(client, "disabled")
        uid = db.get_user_by_username(settings.db_path, "disabled")["id"]
        db.update_user(settings.db_path, uid, is_active=0)
        r_disabled = client.post("/api/auth/refresh", cookies={"refresh_token": rt2})
        assert r_disabled.status_code == 401
        d_disabled = r_disabled.json()["detail"]

        # One single generic detail for every path — no token state leaked.
        assert d_no_cookie == d_revoked == d_disabled == "invalid or expired session"
        assert "revoked" not in d_no_cookie.lower()
        assert "user" not in d_no_cookie.lower()

    # ------------------------------------------------------------------
    # F3 — same-origin guard on the refresh endpoint
    # ------------------------------------------------------------------

    def test_refresh_cross_origin_denied_and_session_survives(self, client):
        """F3: a foreign Origin header gets 403 and must NOT destroy the
        session — the cookie still rotates on the next same-origin call."""
        _register_trusted_lan(client)
        _, rt = _get_tokens(client)
        r = client.post(
            "/api/auth/refresh",
            cookies={"refresh_token": rt},
            headers={"Origin": "https://evil.example"},
        )
        assert r.status_code == 403
        # False positive must not have revoked/rotated the token.
        r2 = client.post("/api/auth/refresh", cookies={"refresh_token": rt})
        assert r2.status_code == 200
        assert r2.json()["access_token"]
        rt2 = _get_refresh_cookie(client)
        assert rt2 is not None and rt2 != rt

    def test_refresh_same_origin_allowed(self, client):
        """F3: an Origin matching the request host passes unchanged
        (TestClient host is ``testserver``)."""
        _register_trusted_lan(client)
        _, rt = _get_tokens(client)
        r = client.post(
            "/api/auth/refresh",
            cookies={"refresh_token": rt},
            headers={"Origin": "http://testserver"},
        )
        assert r.status_code == 200

    def test_refresh_null_origin_rejected(self, client):
        """F3: ``Origin: null`` (sandboxed iframes, redirects, data: docs)
        is never same-origin and must be rejected — the session survives."""
        _register_trusted_lan(client)
        _, rt = _get_tokens(client)
        r = client.post(
            "/api/auth/refresh",
            cookies={"refresh_token": rt},
            headers={"Origin": "null"},
        )
        assert r.status_code == 403
        r2 = client.post("/api/auth/refresh", cookies={"refresh_token": rt})
        assert r2.status_code == 200

    def test_refresh_explicit_port_mismatch_rejected(self, client_localhost_port):
        """F3: same hostname but explicit, differing ports is cross-origin —
        ``http://localhost:5173`` (SPA dev server) must not match a request
        to ``http://localhost:8000`` (API) when both ports are explicit."""
        c = client_localhost_port
        _register_trusted_lan(c)
        _, rt = _get_tokens(c)
        r = c.post(
            "/api/auth/refresh",
            cookies={"refresh_token": rt},
            headers={"Origin": "http://localhost:5173"},
        )
        assert r.status_code == 403
        # Same hostname AND the same explicit port is fine.
        r2 = c.post(
            "/api/auth/refresh",
            cookies={"refresh_token": rt},
            headers={"Origin": "http://localhost:8000"},
        )
        assert r2.status_code == 200

    def test_refresh_foreign_origin_spam_does_not_consume_rate_bucket(self, client):
        """F3: the origin guard runs BEFORE the rate-limit check — repeated
        cross-origin 403s must not count against the 5/minute refresh bucket."""
        _register_trusted_lan(client)
        _, rt = _get_tokens(client)
        for _ in range(5):
            r = client.post(
                "/api/auth/refresh",
                cookies={"refresh_token": rt},
                headers={"Origin": "https://evil.example"},
            )
            assert r.status_code == 403
        # Bucket untouched: the first legitimately-counted, same-origin/none
        # refresh still succeeds.
        r = client.post("/api/auth/refresh", cookies={"refresh_token": rt})
        assert r.status_code == 200
        assert r.json()["access_token"]

    def test_refresh_allows_cors_whitelisted_origin(self, client_cors):
        """NEW-1: split-origin SPA — an Origin listed in METATRACE_CORS_ORIGINS
        passes the guard even though it differs from the API host (regression:
        before this fix every browser refresh 403'd and silently logged the
        user out). Scheme stays significant: https://localhost:5173 is NOT the
        whitelisted http://localhost:5173."""
        _register_trusted_lan(client_cors)
        _, rt = _get_tokens(client_cors)
        r = client_cors.post(
            "/api/auth/refresh",
            cookies={"refresh_token": rt},
            headers={"Origin": "http://localhost:5173"},
        )
        assert r.status_code == 200
        assert r.json()["access_token"]
        r2 = client_cors.post(
            "/api/auth/refresh",
            cookies={"refresh_token": rt},
            headers={"Origin": "https://localhost:5173"},
        )
        assert r2.status_code == 403

    def test_refresh_error_responses_are_no_store(self, client):
        """NEW-3: refresh failures must carry ``Cache-Control: no-store`` so a
        shared cache can't replay a stale 403/401 auth decision."""
        _register_trusted_lan(client)
        _, rt = _get_tokens(client)
        r403 = client.post(
            "/api/auth/refresh",
            cookies={"refresh_token": rt},
            headers={"Origin": "https://evil.example"},
        )
        assert r403.status_code == 403
        assert r403.headers.get("cache-control") == "no-store, max-age=0"
        r401 = client.post("/api/auth/refresh")
        assert r401.status_code == 401
        assert r401.headers.get("cache-control") == "no-store, max-age=0"

    # ------------------------------------------------------------------
    # Expired access token -> silent refresh -> retry contract
    # ------------------------------------------------------------------

    def test_expired_access_token_refresh_retry_contract(self, client, settings):
        """The exact client-side flow: an expired access token 401s /api/stats,
        POST /api/auth/refresh mints a new access token, and the retry with
        that token succeeds."""
        _register_trusted_lan(client)
        _, rt = _get_tokens(client)
        expired = create_access_token(1, "viewer", secret=settings.jwt_secret, ttl_minutes=-1)

        # Expired JWT -> 401 before the endpoint body runs.
        r1 = client.get("/api/stats", headers=_auth_headers(expired))
        assert r1.status_code == 401
        assert r1.json()["detail"] == "invalid or expired token"

        # Silent refresh with the httpOnly cookie.
        r2 = client.post("/api/auth/refresh", cookies={"refresh_token": rt})
        assert r2.status_code == 200
        new_at = r2.json()["access_token"]
        assert new_at and new_at != expired

        # Retry the original request with the fresh token.
        r3 = client.get("/api/stats", headers=_auth_headers(new_at))
        assert r3.status_code == 200

    # ------------------------------------------------------------------
    # per_endpoint rate-limit isolation: /api/stats vs /api/auth/refresh
    # ------------------------------------------------------------------

    def test_refresh_rate_limit_isolated_from_stats(self, client):
        """Exhausting the /api/stats bucket (30/min per IP:path) must NOT 429
        /api/auth/refresh — the per_endpoint=True scoping keeps them apart."""
        _register_trusted_lan(client)
        at, rt = _get_tokens(client)
        headers = _auth_headers(at)
        statuses = [client.get("/api/stats", headers=headers).status_code for _ in range(31)]
        assert statuses[-1] == 429, f"expected /api/stats to be rate-limited: {statuses[-5:]}"
        # Refresh keeps its own 5/minute bucket — one call must still succeed.
        r = client.post("/api/auth/refresh", cookies={"refresh_token": rt})
        assert r.status_code == 200


# ===========================================================================
# LOGOUT ENDPOINT TESTS
# ===========================================================================

class TestLogout:
    def test_logout_clears_cookie(self, client):
        _register_trusted_lan(client)
        at, rt = _get_tokens(client)
        r = client.post("/api/auth/logout",
                        cookies={"refresh_token": rt},
                        headers=_auth_headers(at))
        assert r.status_code == 200
        set_cookie = r.headers.get("set-cookie", "")
        assert "refresh_token=" in set_cookie

    def test_logout_revokes_refresh_token(self, client):
        _register_trusted_lan(client)
        at, rt = _get_tokens(client)
        client.post("/api/auth/logout",
                    cookies={"refresh_token": rt},
                    headers=_auth_headers(at))
        r = client.post("/api/auth/refresh", cookies={"refresh_token": rt})
        assert r.status_code == 401

    def test_logout_requires_auth(self, client):
        r = client.post("/api/auth/logout")
        assert r.status_code == 401


# ===========================================================================
# PASSWORD CHANGE TESTS
# ===========================================================================

class TestChangePassword:
    def test_change_password_success(self, client):
        _register_trusted_lan(client)
        at, _ = _get_tokens(client)
        r = client.post("/api/auth/change-password", json={
            "current_password": "Secure-Password-123",
            "new_password": "New-Password-456789",
        }, headers=_auth_headers(at))
        assert r.status_code == 200
        r_login = _login(client, password="Secure-Password-123")
        assert r_login.status_code == 401
        r_login2 = _login(client, password="New-Password-456789")
        assert r_login2.status_code == 200

    def test_change_password_revokes_all_tokens(self, client):
        _register_trusted_lan(client)
        at, rt = _get_tokens(client)
        client.post("/api/auth/change-password", json={
            "current_password": "Secure-Password-123",
            "new_password": "New-Password-456789",
        }, headers=_auth_headers(at))
        r = client.post("/api/auth/refresh", cookies={"refresh_token": rt})
        assert r.status_code == 401

    def test_change_password_wrong_current(self, client):
        _register_trusted_lan(client)
        at, _ = _get_tokens(client)
        r = client.post("/api/auth/change-password", json={
            "current_password": "wrong-old-password",
            "new_password": "New-Password-456789",
        }, headers=_auth_headers(at))
        assert r.status_code == 400


# ===========================================================================
# RBAC TESTS
# ===========================================================================

class TestRBAC:
    @pytest.fixture()
    def roles_client(self, app_admin):
        """Create admin, editor, viewer with real JWTs."""
        c, _ = app_admin
        tok = "admintoken1234567890123456789012"
        _register_admin_token(c, tok, username="adm_u", email="adm@test.com", role="admin")
        _register_admin_token(c, tok, username="edt_u", email="edt@test.com", role="editor")
        _register_admin_token(c, tok, username="vew_u", email="vew@test.com", role="viewer")
        at_admin, _ = _get_tokens(c, "adm_u")
        at_editor, _ = _get_tokens(c, "edt_u")
        at_viewer, _ = _get_tokens(c, "vew_u")
        return c, {"admin": at_admin, "editor": at_editor, "viewer": at_viewer}

    def test_unauthenticated_cannot_rescan(self, app_admin):
        c, _ = app_admin
        r = c.post("/api/rescan")
        assert r.status_code in (401, 403)

    def test_viewer_can_search(self, client):
        _register_trusted_lan(client)
        r = client.post("/api/search", params={"q": "image"})
        assert r.status_code == 200

    def test_viewer_can_browse(self, client):
        _register_trusted_lan(client)
        r = client.get("/api/images")
        assert r.status_code == 200

    def test_viewer_cannot_rescan(self, roles_client):
        c, tokens = roles_client
        r = c.post("/api/rescan", headers=_auth_headers(tokens["viewer"]))
        assert r.status_code == 403

    def test_editor_can_rescan(self, roles_client):
        c, tokens = roles_client
        r = c.post("/api/rescan", headers=_auth_headers(tokens["editor"]))
        assert r.status_code in (202, 409)

    def test_admin_can_manage_users(self, roles_client):
        c, tokens = roles_client
        r = c.get("/api/users", headers=_auth_headers(tokens["admin"]))
        assert r.status_code == 200

    def test_editor_cannot_manage_users(self, roles_client):
        c, tokens = roles_client
        r = c.get("/api/users", headers=_auth_headers(tokens["editor"]))
        assert r.status_code == 403

    def test_viewer_cannot_manage_users(self, roles_client):
        c, tokens = roles_client
        r = c.get("/api/users", headers=_auth_headers(tokens["viewer"]))
        assert r.status_code == 403


# ===========================================================================
# USER MANAGEMENT (ADMIN) TESTS
# ===========================================================================

class TestUserManagement:
    @pytest.fixture()
    def admin_client(self, app_admin):
        """Admin client with real JWT.

        Logs in as the seeded admin (username='admin', password=admin_token)
        so there is exactly one admin in the DB — the last-admin guard must
        not be bypassed by a second auto-created admin.

        The seeded admin has ``must_change_password=True`` (see plan-frontend
        §1.3) and the server-side gate blocks all non-whitelisted endpoints
        until the flag is cleared. These tests don't exercise that gate, so
        we clear it directly via the DB helper to keep their setup terse.
        """
        c, settings = app_admin
        at, _ = _get_tokens(c, "admin", "admintoken1234567890123456789012")
        # Clear must_change_password directly so these legacy tests don't
        # trip the server-side gate. The dedicated TestMustChangePassword
        # class exercises the gate end-to-end. Scope to the seeded admin
        # only so future user-management tests that create extra users are
        # not affected by this blanket reset.
        with db.connect(settings.db_path) as conn:
            conn.execute("UPDATE users SET must_change_password = 0 WHERE username = 'admin'")
        return c, at, settings

    def test_list_users(self, admin_client):
        c, at, _ = admin_client
        r = c.get("/api/users", headers=_auth_headers(at))
        assert r.status_code == 200
        users = r.json()["users"]
        assert len(users) >= 1

    def test_admin_cannot_delete_last_admin(self, admin_client):
        c, at, _ = admin_client
        me = c.get("/api/auth/me", headers=_auth_headers(at))
        uid = me.json()["user"]["id"]
        r = c.delete(f"/api/users/{uid}", headers=_auth_headers(at))
        assert r.status_code == 400
        assert "last admin" in r.json()["detail"]

    def test_create_user_via_api(self, admin_client):
        c, at, _ = admin_client
        r = c.post("/api/users", json={
            "username": "newuser", "email": "new@test.com",
            "password": "New-Pass-123456", "role": "editor",
        }, headers=_auth_headers(at))
        assert r.status_code == 201
        assert r.json()["username"] == "newuser"
        assert r.json()["role"] == "editor"

    def test_deactivate_user_revokes_tokens(self, admin_client):
        c, at, settings = admin_client
        # Create a user to deactivate
        _register_admin_token(c, "admintoken1234567890123456789012",
                              username="deact", email="deact@test.com", role="viewer")
        _, rt_viewer = _get_tokens(c, "deact")
        # Get viewer's id
        me = c.get("/api/auth/me", headers=_auth_headers(_get_tokens(c, "deact")[0]))
        uid = me.json()["user"]["id"]
        # Deactivate
        r = c.patch(f"/api/users/{uid}", json={"is_active": False},
                    headers=_auth_headers(at))
        assert r.status_code == 200
        # Viewer's refresh token should not work
        r2 = c.post("/api/auth/refresh", cookies={"refresh_token": rt_viewer})
        assert r2.status_code == 401

    def test_user_create_rate_limited(self, admin_client):
        """L-2: 11th user-create within a minute is rate-limited (10/min)."""
        c, at, _ = admin_client
        for i in range(10):
            r = c.post("/api/users", json={
                "username": f"rl{i}", "email": f"rl{i}@test.com",
                "password": "Good-Password-123", "role": "viewer",
            }, headers=_auth_headers(at))
            assert r.status_code == 201, f"request {i} failed: {r.status_code} {r.text}"
        # 11th must be 429
        r = c.post("/api/users", json={
            "username": "rl_overflow", "email": "overflow@test.com",
            "password": "Good-Password-123", "role": "viewer",
        }, headers=_auth_headers(at))
        assert r.status_code == 429


# ===========================================================================
# ACCOUNT LOCKOUT TESTS
# ===========================================================================

class TestAccountLockout:
    def test_lockout_after_max_failures(self, app_admin):
        c, settings = app_admin
        _register_admin_token(c, "admintoken1234567890123456789012",
                              username="lockme", email="lock@test.com",
                              password="Good-Password-123")
        for i in range(5):
            r = _login(c, username="lockme", password=f"bad-{i}")
            assert r.status_code == 401
        r = _login(c, username="lockme", password="Good-Password-123")
        assert r.status_code == 423

    def test_successful_login_resets_failures(self, app_admin):
        c, settings = app_admin
        _register_admin_token(c, "admintoken1234567890123456789012",
                              username="resetme", email="reset@test.com",
                              password="Good-Password-123")
        for i in range(3):
            r = _login(c, username="resetme", password=f"bad-{i}")
            assert r.status_code == 401
        # Successful login resets counter
        r = _login(c, username="resetme", password="Good-Password-123")
        assert r.status_code == 200
        for i in range(3):
            r = _login(c, username="resetme", password=f"bad-{i}")
            assert r.status_code == 401
        r = _login(c, username="resetme", password="Good-Password-123")
        assert r.status_code == 200


# ===========================================================================
# AUDIT LOG TESTS
# ===========================================================================

class TestAuditLog:
    def test_login_creates_audit_entry(self, client, settings):
        _register_trusted_lan(client)
        r = _login(client)
        assert r.status_code == 200
        with db.connect(settings.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM audit_log WHERE action = 'login' ORDER BY id DESC LIMIT 1"
            ).fetchall()
        assert len(rows) == 1
        assert rows[0]["user_id"] is not None

    def test_failed_login_creates_audit_entry(self, client, settings):
        _register_trusted_lan(client)
        r = _login(client, password="wrong")
        assert r.status_code == 401
        with db.connect(settings.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM audit_log WHERE action = 'login_failed' ORDER BY id DESC LIMIT 1"
            ).fetchall()
        assert len(rows) == 1


# ===========================================================================
# SECURITY EDGE CASES
# ===========================================================================

class TestSecurityEdgeCases:
    def test_no_sql_injection_in_login(self, client):
        _register_trusted_lan(client)
        r = client.post("/api/auth/login", json={
            "username": "' OR '1'='1",
            "password": "' OR '1'='1",
        })
        assert r.status_code == 401

    def test_error_messages_dont_leak_info(self, client):
        r = _login(client, username="nonexistent", password="wrong")
        assert r.status_code == 401
        assert "nonexistent" not in r.json().get("detail", "")

    def test_password_hash_not_in_response(self, client):
        _register_trusted_lan(client)
        r = _login(client)
        body = r.json()
        assert "password" not in body
        assert "password_hash" not in str(body)

    def test_token_has_expiration(self, settings):
        token = create_access_token(1, "admin", secret=settings.jwt_secret, ttl_minutes=15)
        payload = decode_access_token(token, secret=settings.jwt_secret)
        exp = datetime.fromtimestamp(payload["exp"], tz=timezone.utc)
        iat = datetime.fromtimestamp(payload["iat"], tz=timezone.utc)
        assert exp > iat
        assert (exp - iat).total_seconds() <= 16 * 60

    def test_refresh_token_ttl_configurable(self, settings):
        assert settings.refresh_token_ttl_days == 7

    def test_refresh_cookie_httponly(self, client):
        _register_trusted_lan(client)
        r = _login(client)
        assert r.status_code == 200
        for header_val in r.headers.get_list("set-cookie"):
            if "refresh_token=" in header_val:
                assert "httponly" in header_val.lower()
                return
        pytest.fail("refresh_token cookie not found in Set-Cookie headers")

    def test_refresh_cookie_path_scoped(self, client):
        _register_trusted_lan(client)
        r = _login(client)
        assert r.status_code == 200
        for header_val in r.headers.get_list("set-cookie"):
            if "refresh_token=" in header_val:
                assert "path=/api/auth/refresh" in header_val.lower().replace(" ", "")
                return
        pytest.fail("refresh_token cookie not found in Set-Cookie headers")


# ===========================================================================
# MUST-CHANGE-PASSWORD FLAG TESTS
# ===========================================================================

class TestMustChangePassword:
    """The seeded admin (literal ``changeme`` path) carries the
    ``must_change_password`` flag.  Three guarantees:

    1. The flag is set on the seeded user.
    2. ``POST /api/auth/change-password`` clears it.
    3. Every non-whitelisted authenticated endpoint 403s while it's set.
    """

    @pytest.fixture()
    def app_seed(self, tmp_path, monkeypatch):
        """App with no admin_token but auth required — the seed path uses the
        literal ``DEFAULT_ADMIN_PASSWORD = "changeme"`` so the flag is set on
        the freshly-seeded admin (plan-frontend §1.3)."""
        monkeypatch.setattr(embeddings, "embed_images", _embed_fake)
        monkeypatch.setattr(metadata, "extract_xmp", lambda paths: {})
        store = tmp_path / "store"
        store.mkdir()
        Image.new("RGB", (8, 8), (1, 2, 3)).save(store / "x.png")
        settings = Settings(
            store_path=store,
            data_path=tmp_path / "data",
            run_initial_scan_on_start=False,
            batch_size=8,
            jwt_secret="test-jwt-secret-32-chars-minimum-length!!",
            admin_token=None,
            allow_unauthenticated=False,
        )
        Indexer(settings).incremental(trigger="seed")
        _app = create_app(settings)
        with TestClient(_app) as c:
            yield c, settings

    def test_seed_admin_has_flag_set(self, app_seed):
        """Empty DB + no admin_token → seeded admin must carry the flag."""
        c, settings = app_seed
        # Sanity: the users table is non-empty (seeding ran) and the seeded
        # user is the only row, with must_change_password=1.
        with db.connect(settings.db_path) as conn:
            row = conn.execute(
                "SELECT must_change_password FROM users WHERE username = 'admin'"
            ).fetchone()
        assert row is not None and row["must_change_password"] == 1

        # Log in with the literal default password — the seed value bypasses
        # the operator-enforced complexity rules because it's the one-shot
        # bootstrap credential.
        at, _ = _get_tokens(c, "admin", "changeme")
        r = c.get("/api/auth/me", headers=_auth_headers(at))
        assert r.status_code == 200
        body = r.json()
        assert body["must_change_password"] is True
        assert body["user"]["username"] == "admin"

    def test_flag_cleared_after_change_password(self, app_seed):
        """After a successful change-password, /me reports the flag cleared."""
        c, _ = app_seed
        at, _ = _get_tokens(c, "admin", "changeme")

        # Sanity: flag is set before the change.
        me_before = c.get("/api/auth/me", headers=_auth_headers(at))
        assert me_before.json()["must_change_password"] is True

        # Change the password. Server-side validator enforces >=12 chars
        # with upper, lower, digit.
        r = c.post("/api/auth/change-password", json={
            "current_password": "changeme",
            "new_password": "New-Password-12345",
        }, headers=_auth_headers(at))
        assert r.status_code == 200

        # /me must now report the flag as cleared. Re-login first because
        # change-password revokes all refresh tokens.
        at2, _ = _get_tokens(c, "admin", "New-Password-12345")
        me_after = c.get("/api/auth/me", headers=_auth_headers(at2))
        assert me_after.status_code == 200
        assert me_after.json()["must_change_password"] is False

    def test_non_whitelisted_endpoint_403_when_flag_set(self, app_seed):
        """The server-side gate blocks every endpoint that isn't in the
        whitelist. The whitelist itself must still work."""
        c, _ = app_seed
        at, _ = _get_tokens(c, "admin", "changeme")

        # Non-whitelisted: /api/stats is gated by require_role(admin,editor,viewer)
        # so it goes through get_current_user → _decode_user_row → gate.
        r = c.get("/api/stats", headers=_auth_headers(at))
        assert r.status_code == 403
        assert r.json()["detail"] == "password_change_required"

        # Whitelisted: /api/auth/me
        r_me = c.get("/api/auth/me", headers=_auth_headers(at))
        assert r_me.status_code == 200

        # Whitelisted: /api/auth/logout — best-effort ok; the gate must not
        # block it even though the session is about to end.
        r_out = c.post("/api/auth/logout", headers=_auth_headers(at))
        assert r_out.status_code == 200

        # Whitelisted: /api/auth/change-password. Re-login first because
        # logout cleared our refresh token and the JWT is single-use.
        at2, _ = _get_tokens(c, "admin", "changeme")
        r_cp = c.post("/api/auth/change-password", json={
            "current_password": "changeme",
            "new_password": "New-Password-12345",
        }, headers=_auth_headers(at2))
        assert r_cp.status_code == 200
