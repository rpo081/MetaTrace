"""Tests for TOTP two-factor authentication (opt-in, app behaviour unchanged)."""
from __future__ import annotations

import time

import numpy as np
import pyotp
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from PIL import Image

from backend.app import db, embeddings, metadata
from backend.app.config import Settings
from backend.app.indexer import Indexer
from backend.app.main import create_app
from backend.app.mfa import (
    create_mfa_token,
    decrypt_secret,
    encrypt_secret,
    generate_backup_codes,
    get_fernet,
    normalize_backup_code,
    verify_backup_code_hash,
    verify_totp,
)


MFA_KEY = Fernet.generate_key().decode()
JWT_SECRET = "test-jwt-secret-32-chars-minimum-length!!"


def _embed_fake(images, settings):
    v = np.ones((len(images), 8), dtype=np.float32)
    return v / np.linalg.norm(v)


@pytest.fixture()
def app(tmp_path, monkeypatch):
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
        jwt_secret=JWT_SECRET,
        allow_unauthenticated=True,
        mfa_encryption_key=MFA_KEY,
    )
    Indexer(settings).incremental(trigger="seed")
    _app = create_app(settings)
    with TestClient(_app) as c:
        yield c, settings


@pytest.fixture()
def client(app):
    return app[0]


@pytest.fixture()
def settings(app):
    return app[1]


@pytest.fixture()
def totp_clock(monkeypatch):
    """Deterministic TOTP step control.

    Patches ``current_counter`` in the MFA router so confirm/verify tests
    don't depend on wall-clock 30 s boundaries. The box starts at the real
    step, so ``pyotp`` crypto checks (``valid_window=1``) still pass while
    the replay guard sees exactly the steps we set.
    """
    import backend.app.api.mfa as mfa_api

    box = {"step": int(time.time() // 30)}
    monkeypatch.setattr(mfa_api, "current_counter", lambda: box["step"])
    return box


def code_at(secret: str, step: int) -> str:
    return pyotp.TOTP(secret).at(step * 30)


def _register(client, username="mfauser", password="Good-Password-123", role="viewer"):
    return client.post("/api/auth/register", json={
        "username": username, "email": f"{username}@test.com",
        "password": password, "role": role,
    })


def _login(client, username="mfauser", password="Good-Password-123"):
    return client.post("/api/auth/login", json={"username": username, "password": password})


def _auth(username="mfauser", password="Good-Password-123", client=None):
    r = _login(client, username, password)
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _enroll_and_confirm(client, headers, clock=None):
    e = client.post("/api/auth/mfa/enroll", headers=headers)
    assert e.status_code == 200, e.text
    assert e.headers.get("cache-control") == "no-store, max-age=0"
    secret = e.json()["secret"]
    code = code_at(secret, clock["step"]) if clock else pyotp.TOTP(secret).now()
    c = client.post("/api/auth/mfa/confirm", headers=headers, json={"code": code})
    assert c.status_code == 200, c.text
    assert c.headers.get("cache-control") == "no-store, max-age=0"
    return secret, c.json()["backup_codes"]


class TestTotpPrimitives:
    def test_rfc6238_vector(self):
        """RFC 6238 Appendix B: SHA1 secret, T=59 → 287082 (8-digit 94287082)."""
        secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # base32("12345678901234567890")
        assert pyotp.TOTP(secret, digits=8).at(59) == "94287082"
        assert verify_totp(secret, pyotp.TOTP(secret).now()) is True

    def test_verify_rejects_garbage(self):
        secret = pyotp.random_base32()
        assert verify_totp(secret, "abcdef") is False
        assert verify_totp(secret, "") is False
        assert verify_totp(secret, "000000") is False

    def test_fernet_roundtrip(self):
        f = get_fernet(MFA_KEY)
        enc = encrypt_secret(f, "JBSWY3DPEHPK3PXP")
        assert enc != "JBSWY3DPEHPK3PXP"
        assert decrypt_secret(f, enc) == "JBSWY3DPEHPK3PXP"

    def test_fernet_missing_key(self):
        with pytest.raises(ValueError, match="not configured"):
            get_fernet(None)
        with pytest.raises(ValueError, match="not configured"):
            get_fernet("")

    def test_backup_code_format_and_hash(self):
        codes = generate_backup_codes(10)
        assert len(set(codes)) == 10
        # XXXX-XXXX-XXXX, 48 bits entropy
        assert all(len(c) == 14 and c[4] == "-" and c[9] == "-" for c in codes)
        assert normalize_backup_code(codes[0].lower().replace("-", "")) == \
            normalize_backup_code(codes[0])
        # Argon2id hash: salted, slow, verifies via helper, never plaintext.
        from backend.app.mfa import hash_backup_code

        h = hash_backup_code(codes[0])
        assert codes[0] not in h and normalize_backup_code(codes[0]) not in h
        assert verify_backup_code_hash(codes[0], h) is True
        assert verify_backup_code_hash("0000-0000-0000", h) is False


class TestNoMfaRegression:
    def test_login_shape_unchanged_without_mfa(self, client):
        _register(client)
        r = _login(client)
        assert r.status_code == 200
        body = r.json()
        assert set(body.keys()) == {"access_token", "token_type", "user"}
        assert "mfa_required" not in body
        assert "refresh_token=" in r.headers.get("set-cookie", "")

    def test_me_reports_mfa_disabled(self, client):
        _register(client)
        h = _auth(client=client)
        me = client.get("/api/auth/me", headers=h)
        assert me.status_code == 200
        assert me.json()["mfa_enabled"] is False

    def test_status_defaults(self, client):
        _register(client)
        h = _auth(client=client)
        s = client.get("/api/auth/mfa/status", headers=h)
        assert s.status_code == 200
        assert s.json() == {"enabled": False, "enrolled_at": None,
                            "backup_remaining": 0, "has_pending": False}


class TestEnrollConfirm:
    def test_enroll_confirm_roundtrip(self, client, settings):
        _register(client)
        h = _auth(client=client)
        secret, codes = _enroll_and_confirm(client, h)
        assert len(codes) == 10
        row = db.get_user_by_username(settings.db_path, "mfauser")
        assert row["mfa_enabled"] == 1
        assert row["mfa_secret"] != secret  # encrypted at rest
        assert row["mfa_secret"] is not None
        assert db.count_unused_mfa_backup_codes(settings.db_path, row["id"]) == 10

    def test_enroll_overwrite_kills_old_secret(self, client):
        """A second enroll replaces the pending secret; the old QR/code dies."""
        _register(client)
        h = _auth(client=client)
        old_secret = client.post("/api/auth/mfa/enroll", headers=h).json()["secret"]
        new_secret = client.post("/api/auth/mfa/enroll", headers=h).json()["secret"]
        assert new_secret != old_secret
        assert client.post("/api/auth/mfa/confirm", headers=h,
                           json={"code": pyotp.TOTP(old_secret).now()}).status_code == 401
        assert client.post("/api/auth/mfa/confirm", headers=h,
                           json={"code": pyotp.TOTP(new_secret).now()}).status_code == 200

    def test_qr_is_png_no_store(self, client):
        _register(client)
        h = _auth(client=client)
        client.post("/api/auth/mfa/enroll", headers=h)
        q = client.get("/api/auth/mfa/qr", headers=h)
        assert q.status_code == 200
        assert q.headers["content-type"] == "image/png"
        assert q.content[:8] == b"\x89PNG\r\n\x1a\n"
        assert q.headers.get("cache-control") == "no-store, max-age=0"

    def test_qr_no_pending_404(self, client):
        _register(client)
        h = _auth(client=client)
        r = client.get("/api/auth/mfa/qr", headers=h)
        assert r.status_code == 404

    def test_qr_already_enabled_400(self, client, totp_clock):
        _register(client)
        h = _auth(client=client)
        _enroll_and_confirm(client, h, totp_clock)
        assert client.get("/api/auth/mfa/qr", headers=h).status_code == 400

    def test_confirm_wrong_code(self, client):
        _register(client)
        h = _auth(client=client)
        client.post("/api/auth/mfa/enroll", headers=h)
        r = client.post("/api/auth/mfa/confirm", headers=h, json={"code": "wrongcode"})
        assert r.status_code == 401

    def test_confirm_no_pending_400(self, client):
        _register(client)
        h = _auth(client=client)
        r = client.post("/api/auth/mfa/confirm", headers=h, json={"code": "123456"})
        assert r.status_code == 400

    def test_confirm_failures_feed_lockout(self, client):
        _register(client, username="clockc")
        h = _auth(username="clockc", client=client)
        client.post("/api/auth/mfa/enroll", headers=h)
        for _ in range(5):
            assert client.post("/api/auth/mfa/confirm", headers=h,
                               json={"code": "wrongcode"}).status_code == 401
        locked = _login(client, username="clockc")
        assert locked.status_code == 423

    def test_enroll_when_already_enabled(self, client, totp_clock):
        _register(client)
        h = _auth(client=client)
        _enroll_and_confirm(client, h, totp_clock)
        r = client.post("/api/auth/mfa/enroll", headers=h)
        assert r.status_code == 400

    def test_mfa_unconfigured_500(self, tmp_path, monkeypatch):
        monkeypatch.setattr(embeddings, "embed_images", _embed_fake)
        monkeypatch.setattr(metadata, "extract_xmp", lambda paths: {})
        store = tmp_path / "store"
        store.mkdir()
        Image.new("RGB", (8, 8), (1, 2, 3)).save(store / "x.png")
        s = Settings(store_path=store, data_path=tmp_path / "data",
                     run_initial_scan_on_start=False, batch_size=8,
                     jwt_secret=JWT_SECRET, allow_unauthenticated=True)
        Indexer(s).incremental(trigger="seed")
        with TestClient(create_app(s)) as c:
            c.post("/api/auth/register", json={"username": "user2", "email": "u2@t.com",
                                                "password": "Good-Password-123", "role": "viewer"})
            at = c.post("/api/auth/login",
                        json={"username": "user2", "password": "Good-Password-123"}).json()["access_token"]
            r = c.post("/api/auth/mfa/enroll",
                       headers={"Authorization": f"Bearer {at}"})
            assert r.status_code == 500
            assert r.json()["detail"] == "MFA not configured"


class TestLoginVerify:
    def test_login_returns_challenge_without_cookies(self, client, totp_clock):
        _register(client)
        h = _auth(client=client)
        _enroll_and_confirm(client, h, totp_clock)
        r = _login(client)
        assert r.status_code == 200
        body = r.json()
        assert body.get("mfa_required") is True
        assert "mfa_token" in body
        assert "access_token" not in body
        assert "refresh_token=" not in r.headers.get("set-cookie", "")

    def test_verify_full_session(self, client, totp_clock):
        _register(client)
        h = _auth(client=client)
        secret, _ = _enroll_and_confirm(client, h, totp_clock)
        totp_clock["step"] += 1  # confirm seeded the old step; log in on the next
        mt = _login(client).json()["mfa_token"]
        v = client.post("/api/auth/mfa/verify",
                        json={"mfa_token": mt, "code": code_at(secret, totp_clock["step"])})
        assert v.status_code == 200, v.text
        assert "access_token" in v.json()
        me = client.get("/api/auth/me",
                        headers={"Authorization": f"Bearer {v.json()['access_token']}"})
        assert me.status_code == 200
        assert me.json()["mfa_enabled"] is True

    def test_mfa_token_rejected_as_access_token(self, client, totp_clock):
        """F1 regression: a pre-auth token must never authenticate API calls."""
        _register(client)
        h = _auth(client=client)
        _enroll_and_confirm(client, h, totp_clock)
        mt = _login(client).json()["mfa_token"]
        assert client.get("/api/stats",
                          headers={"Authorization": f"Bearer {mt}"}).status_code == 401
        assert client.get("/api/auth/me",
                          headers={"Authorization": f"Bearer {mt}"}).status_code == 401

    def test_verify_expired_token_rejected(self, client, totp_clock):
        _register(client)
        h = _auth(client=client)
        secret, _ = _enroll_and_confirm(client, h, totp_clock)
        uid = client.get("/api/auth/me", headers=h).json()["user"]["id"]
        expired = create_mfa_token(uid, secret=JWT_SECRET, ttl_minutes=-1)
        totp_clock["step"] += 1
        r = client.post("/api/auth/mfa/verify",
                        json={"mfa_token": expired,
                              "code": code_at(secret, totp_clock["step"])})
        assert r.status_code == 401

    def test_verify_rate_limited(self, client):
        statuses = [client.post(
            "/api/auth/mfa/verify",
            json={"mfa_token": "not-a-valid-token-123", "code": "123456"},
        ).status_code for _ in range(6)]
        assert statuses[:5] == [401] * 5
        assert statuses[5] == 429

    def test_verify_replay_token_single_use(self, client, totp_clock):
        _register(client)
        h = _auth(client=client)
        secret, _ = _enroll_and_confirm(client, h, totp_clock)
        totp_clock["step"] += 1
        code = code_at(secret, totp_clock["step"])
        mt = _login(client).json()["mfa_token"]
        assert client.post("/api/auth/mfa/verify",
                           json={"mfa_token": mt, "code": code}).status_code == 200
        # Same pre-auth token cannot mint a second session.
        r2 = client.post("/api/auth/mfa/verify",
                         json={"mfa_token": mt, "code": code})
        assert r2.status_code == 401

    def test_verify_same_step_replay_rejected(self, client, totp_clock):
        """Atomic CAS guard: same code twice in one step → second fails."""
        _register(client)
        h = _auth(client=client)
        secret, _ = _enroll_and_confirm(client, h, totp_clock)
        totp_clock["step"] += 1
        code = code_at(secret, totp_clock["step"])
        mt1 = _login(client).json()["mfa_token"]
        assert client.post("/api/auth/mfa/verify",
                           json={"mfa_token": mt1, "code": code}).status_code == 200
        # Fresh token, same code, same step → replay rejected.
        mt2 = _login(client).json()["mfa_token"]
        assert client.post("/api/auth/mfa/verify",
                           json={"mfa_token": mt2, "code": code}).status_code == 401
        # (The guard releases on the next step — proven by every
        # test that verifies at box+1 after a confirm-seed, e.g.
        # test_verify_full_session. A +2 jump is untestable here:
        # pyotp crypto only tolerates ±1 around wall-clock time.)

    def test_replay_guard_hermetic(self, client, totp_clock, monkeypatch):
        """Fully wall-clock-independent guard proof (crypto stubbed).

        With ``verify_totp`` mocked, only ``compare_and_set_mfa_counter``
        decides: same step twice → 401, then every next step → 200. Catches
        a permanent-lock-after-verify regression that single-step tests miss.
        """
        import backend.app.api.mfa as mfa_api

        monkeypatch.setattr(mfa_api, "verify_totp", lambda secret, code: True)
        _register(client)
        h = _auth(client=client)
        e = client.post("/api/auth/mfa/enroll", headers=h)
        assert e.status_code == 200
        assert client.post("/api/auth/mfa/confirm", headers=h,
                           json={"code": "111111"}).status_code == 200
        # Seeded step: any code (even the confirm one) is a replay.
        mt0 = _login(client).json()["mfa_token"]
        assert client.post("/api/auth/mfa/verify",
                           json={"mfa_token": mt0, "code": "111111"}).status_code == 401
        # …but each following step mints exactly one session.
        for step_code in ("222222", "333333"):
            totp_clock["step"] += 1
            mt = _login(client).json()["mfa_token"]
            assert client.post("/api/auth/mfa/verify",
                               json={"mfa_token": mt, "code": step_code}).status_code == 200
            mt_replay = _login(client).json()["mfa_token"]
            assert client.post("/api/auth/mfa/verify",
                               json={"mfa_token": mt_replay,
                                     "code": step_code}).status_code == 401

    def test_confirm_code_not_reusable_for_login(self, client, totp_clock):
        """The enrollment proof must not buy a session in the same window."""
        _register(client)
        h = _auth(client=client)
        e = client.post("/api/auth/mfa/enroll", headers=h)
        secret = e.json()["secret"]
        confirm_code = code_at(secret, totp_clock["step"])
        assert client.post("/api/auth/mfa/confirm", headers=h,
                           json={"code": confirm_code}).status_code == 200
        mt = _login(client).json()["mfa_token"]
        assert client.post("/api/auth/mfa/verify",
                           json={"mfa_token": mt, "code": confirm_code}).status_code == 401

    def test_verify_wrong_code_feeds_lockout(self, client, totp_clock):
        _register(client, username="lockmfa")
        h = _auth(username="lockmfa", client=client)
        _enroll_and_confirm(client, h, totp_clock)
        for _ in range(5):
            mt = client.post("/api/auth/login",
                             json={"username": "lockmfa",
                                   "password": "Good-Password-123"}).json()["mfa_token"]
            r = client.post("/api/auth/mfa/verify",
                            json={"mfa_token": mt, "code": "wrongcode"})
            assert r.status_code == 401
        locked = client.post("/api/auth/login",
                             json={"username": "lockmfa",
                                   "password": "Good-Password-123"})
        assert locked.status_code == 423
        assert "locked" in locked.json()["detail"]

    def test_verify_purpose_confusion_rejected(self, client, totp_clock):
        """An access JWT must not be accepted as mfa_token."""
        _register(client)
        h = _auth(client=client)
        _enroll_and_confirm(client, h, totp_clock)
        access = h["Authorization"].split(" ", 1)[1]
        r = client.post("/api/auth/mfa/verify",
                        json={"mfa_token": access, "code": "123456"})
        assert r.status_code == 401

    def test_backup_code_login_and_single_use(self, client, settings, totp_clock):
        _register(client)
        h = _auth(client=client)
        _, codes = _enroll_and_confirm(client, h, totp_clock)
        mt = _login(client).json()["mfa_token"]
        b = client.post("/api/auth/mfa/verify-backup",
                        json={"mfa_token": mt, "code": codes[0]})
        assert b.status_code == 200, b.text
        row = db.get_user_by_username(settings.db_path, "mfauser")
        assert db.count_unused_mfa_backup_codes(settings.db_path, row["id"]) == 9
        # Same backup code cannot be reused.
        mt2 = _login(client).json()["mfa_token"]
        b2 = client.post("/api/auth/mfa/verify-backup",
                         json={"mfa_token": mt2, "code": codes[0]})
        assert b2.status_code == 401
        # Case/dash-insensitive entry works for a fresh code.
        mt3 = _login(client).json()["mfa_token"]
        b3 = client.post("/api/auth/mfa/verify-backup",
                         json={"mfa_token": mt3,
                               "code": codes[1].lower().replace("-", "")})
        assert b3.status_code == 200

    def test_verify_backup_with_totp_code_rejected(self, client, totp_clock):
        _register(client)
        h = _auth(client=client)
        secret, _ = _enroll_and_confirm(client, h, totp_clock)
        totp_clock["step"] += 1
        mt = _login(client).json()["mfa_token"]
        r = client.post("/api/auth/mfa/verify-backup",
                        json={"mfa_token": mt,
                              "code": code_at(secret, totp_clock["step"])})
        assert r.status_code == 401


class TestDisableRegenerate:
    def test_disable_with_password_and_code(self, client, settings, totp_clock):
        _register(client)
        h = _auth(client=client)
        secret, _ = _enroll_and_confirm(client, h, totp_clock)
        totp_clock["step"] += 1
        r = client.post("/api/auth/mfa/disable", headers=h, json={
            "password": "Good-Password-123",
            "code": code_at(secret, totp_clock["step"])})
        assert r.status_code == 200, r.text
        row = db.get_user_by_username(settings.db_path, "mfauser")
        assert row["mfa_enabled"] == 0 and row["mfa_secret"] is None
        # Login is back to single-step.
        assert "access_token" in _login(client).json()

    def test_disable_with_backup_code(self, client, settings, totp_clock):
        _register(client)
        h = _auth(client=client)
        _, codes = _enroll_and_confirm(client, h, totp_clock)
        r = client.post("/api/auth/mfa/disable", headers=h, json={
            "password": "Good-Password-123", "code": codes[0]})
        assert r.status_code == 200, r.text
        row = db.get_user_by_username(settings.db_path, "mfauser")
        assert row["mfa_enabled"] == 0
        assert db.count_unused_mfa_backup_codes(settings.db_path, row["id"]) == 0

    def test_disable_never_enabled_password_only(self, client, settings):
        _register(client)
        h = _auth(client=client)
        client.post("/api/auth/mfa/enroll", headers=h)  # pending only
        r = client.post("/api/auth/mfa/disable", headers=h,
                        json={"password": "Good-Password-123"})
        assert r.status_code == 200, r.text
        row = db.get_user_by_username(settings.db_path, "mfauser")
        assert row["mfa_secret"] is None

    def test_disable_wrong_password(self, client, totp_clock):
        _register(client)
        h = _auth(client=client)
        _enroll_and_confirm(client, h, totp_clock)
        r = client.post("/api/auth/mfa/disable", headers=h,
                        json={"password": "Wrong-Pass-123", "code": "123456"})
        assert r.status_code == 400

    def test_disable_wrong_code_401(self, client, totp_clock):
        _register(client)
        h = _auth(client=client)
        _enroll_and_confirm(client, h, totp_clock)
        r = client.post("/api/auth/mfa/disable", headers=h, json={
            "password": "Good-Password-123", "code": "wrongcode"})
        assert r.status_code == 401

    def test_regenerate_invalidates_old(self, client, settings, totp_clock):
        _register(client)
        h = _auth(client=client)
        secret, old_codes = _enroll_and_confirm(client, h, totp_clock)
        totp_clock["step"] += 1
        r = client.post("/api/auth/mfa/regenerate-codes", headers=h,
                        json={"code": code_at(secret, totp_clock["step"])})
        assert r.status_code == 200, r.text
        assert r.headers.get("cache-control") == "no-store, max-age=0"
        new_codes = r.json()["backup_codes"]
        assert set(new_codes) != set(old_codes)
        mt = _login(client).json()["mfa_token"]
        assert client.post("/api/auth/mfa/verify-backup",
                           json={"mfa_token": mt, "code": old_codes[0]}).status_code == 401

    def test_regenerate_wrong_code_401(self, client, totp_clock):
        _register(client)
        h = _auth(client=client)
        _enroll_and_confirm(client, h, totp_clock)
        r = client.post("/api/auth/mfa/regenerate-codes", headers=h,
                        json={"code": "wrongcode"})
        assert r.status_code == 401


class TestAdminResetAndGate:
    @pytest.fixture()
    def admin_app(self, tmp_path, monkeypatch):
        monkeypatch.setattr(embeddings, "embed_images", _embed_fake)
        monkeypatch.setattr(metadata, "extract_xmp", lambda paths: {})
        store = tmp_path / "store"
        store.mkdir()
        Image.new("RGB", (8, 8), (1, 2, 3)).save(store / "x.png")
        s = Settings(store_path=store, data_path=tmp_path / "data",
                     run_initial_scan_on_start=False, batch_size=8,
                     jwt_secret=JWT_SECRET,
                     admin_token="admintoken1234567890123456789012",
                     mfa_encryption_key=MFA_KEY)
        Indexer(s).incremental(trigger="seed")
        with TestClient(create_app(s)) as c:
            yield c, s

    def _admin_headers(self, c):
        at = c.post("/api/auth/login",
                    json={"username": "admin",
                          "password": "admintoken1234567890123456789012"}).json()["access_token"]
        # Clear the seed must_change_password gate for these tests.
        import backend.app.db as _db
        with _db.connect(c.app.state.settings.db_path) as conn:
            conn.execute("UPDATE users SET must_change_password = 0 WHERE username = 'admin'")
        return {"Authorization": f"Bearer {at}"}

    def test_admin_reset_clears_mfa(self, admin_app, totp_clock):
        c, s = admin_app
        ha = self._admin_headers(c)
        c.post("/api/users", headers=ha,
               json={"username": "victim", "email": "v@t.com",
                     "password": "Good-Password-123", "role": "viewer"})
        at = c.post("/api/auth/login",
                    json={"username": "victim",
                          "password": "Good-Password-123"}).json()["access_token"]
        hv = {"Authorization": f"Bearer {at}"}
        _enroll_and_confirm(c, hv, totp_clock)
        vid = c.get("/api/users", headers=ha).json()["users"]
        vid = next(u["id"] for u in vid if u["username"] == "victim")
        assert c.get(f"/api/users/{vid}", headers=ha).json()["mfa_enabled"] is True
        assert c.post(f"/api/users/{vid}/mfa/reset", headers=ha).status_code == 200
        assert c.get(f"/api/users/{vid}", headers=ha).json()["mfa_enabled"] is False
        # Victim logs in single-step again.
        assert "access_token" in c.post(
            "/api/auth/login",
            json={"username": "victim", "password": "Good-Password-123"}).json()

    def test_admin_reset_404(self, admin_app):
        c, _ = admin_app
        ha = self._admin_headers(c)
        assert c.post("/api/users/9999/mfa/reset", headers=ha).status_code == 404

    def test_admin_reset_requires_admin(self, client):
        _register(client)
        h = _auth(client=client)
        me = client.get("/api/auth/me", headers=h).json()["user"]["id"]
        assert client.post(f"/api/users/{me}/mfa/reset", headers=h).status_code == 403

    def test_reset_password_clears_pending_mfa(self, admin_app):
        """A planted pending secret must not survive an admin credential reset."""
        c, s = admin_app
        ha = self._admin_headers(c)
        c.post("/api/users", headers=ha,
               json={"username": "pending", "email": "p@t.com",
                     "password": "Good-Password-123", "role": "viewer"})
        at = c.post("/api/auth/login",
                    json={"username": "pending",
                          "password": "Good-Password-123"}).json()["access_token"]
        hv = {"Authorization": f"Bearer {at}"}
        pending_secret = c.post("/api/auth/mfa/enroll", headers=hv).json()["secret"]
        vid = next(u["id"] for u in c.get("/api/users", headers=ha).json()["users"]
                   if u["username"] == "pending")
        assert c.post(f"/api/users/{vid}/reset-password", headers=ha,
                      json={"new_password": "New-Password-12345"}).status_code == 200
        # Pending state is gone: even the correct code cannot confirm anymore.
        at2 = c.post("/api/auth/login",
                     json={"username": "pending",
                           "password": "New-Password-12345"}).json()["access_token"]
        hv2 = {"Authorization": f"Bearer {at2}"}
        assert c.post("/api/auth/mfa/confirm", headers=hv2,
                      json={"code": pyotp.TOTP(pending_secret).now()}).status_code == 400

    def test_reset_password_keeps_enabled_mfa(self, admin_app, totp_clock):
        """An active second factor survives a password reset (mfa/reset wipes)."""
        c, s = admin_app
        ha = self._admin_headers(c)
        c.post("/api/users", headers=ha,
               json={"username": "active", "email": "ac@t.com",
                     "password": "Good-Password-123", "role": "viewer"})
        at = c.post("/api/auth/login",
                    json={"username": "active",
                          "password": "Good-Password-123"}).json()["access_token"]
        hv = {"Authorization": f"Bearer {at}"}
        _enroll_and_confirm(c, hv, totp_clock)
        vid = next(u["id"] for u in c.get("/api/users", headers=ha).json()["users"]
                   if u["username"] == "active")
        assert c.post(f"/api/users/{vid}/reset-password", headers=ha,
                      json={"new_password": "New-Password-12345"}).status_code == 200
        body = c.post("/api/auth/login",
                      json={"username": "active",
                            "password": "New-Password-12345"}).json()
        assert body.get("mfa_required") is True

    def test_mfa_gate_opt_in_default_off(self, client):
        """Default (no required roles): unenrolled users see no behaviour change."""
        _register(client)
        h = _auth(client=client)
        assert client.get("/api/stats", headers=h).status_code == 200

    def test_mfa_gate_enforced_for_required_role(self, tmp_path, monkeypatch):
        monkeypatch.setattr(embeddings, "embed_images", _embed_fake)
        monkeypatch.setattr(metadata, "extract_xmp", lambda paths: {})
        store = tmp_path / "store"
        store.mkdir()
        Image.new("RGB", (8, 8), (1, 2, 3)).save(store / "x.png")
        s = Settings(store_path=store, data_path=tmp_path / "data",
                     run_initial_scan_on_start=False, batch_size=8,
                     jwt_secret=JWT_SECRET, allow_unauthenticated=True,
                     mfa_encryption_key=MFA_KEY, mfa_required_roles="admin")
        Indexer(s).incremental(trigger="seed")
        with TestClient(create_app(s)) as c:
            c.post("/api/auth/register",
                   json={"username": "adm2", "email": "a2@t.com",
                         "password": "Good-Password-123", "role": "admin"})
            at = c.post("/api/auth/login",
                        json={"username": "adm2",
                              "password": "Good-Password-123"}).json()["access_token"]
            ha = {"Authorization": f"Bearer {at}"}
            assert c.get("/api/stats", headers=ha).status_code == 403
            assert c.get("/api/stats", headers=ha).json()["detail"] == "mfa_required"
            # Whitelisted paths stay reachable while gated.
            assert c.get("/api/auth/me", headers=ha).status_code == 200
            assert c.get("/api/auth/mfa/status", headers=ha).status_code == 200
            assert c.post("/api/auth/mfa/enroll", headers=ha).status_code == 200
            assert c.post("/api/auth/logout", headers=ha).status_code == 200
