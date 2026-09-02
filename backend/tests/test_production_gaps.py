"""P0 gaps from production audit: query-token RBAC, snapshot admin guard, delta traversal."""
import io
import json
import time
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from backend.app import db, embeddings, metadata
from backend.app.auth import hash_password
from backend.app.config import Settings
from backend.app.indexer import Indexer
from backend.app.main import create_app


def _fake_embed(images, settings):
    v = np.ones((len(images), 8), dtype=np.float32)
    return v / np.linalg.norm(v, axis=1, keepdims=True)


@pytest.fixture()
def auth_app(tmp_path, monkeypatch):
    monkeypatch.setattr(embeddings, "embed_images", _fake_embed)
    monkeypatch.setattr(metadata, "extract_xmp", lambda paths: {})
    store = tmp_path / "store"
    store.mkdir()
    Image.new("RGB", (8, 8), (1, 2, 3)).save(store / "x.png")
    s = Settings(
        store_path=store,
        data_path=tmp_path / "data",
        run_initial_scan_on_start=False,
        batch_size=8,
        jwt_secret="test-jwt-secret-32-chars-minimum-length!!",
        allow_unauthenticated=False,
        cookie_secure=True,
    )
    s.ensure_dirs()
    db.init_db(s.db_path)
    # seed 3 roles
    db.create_user(s.db_path, username="admin", email="a@ex.com", password_hash=hash_password("Abc12345"), role="admin")
    db.create_user(s.db_path, username="ed", email="ed@ex.com", password_hash=hash_password("Abc12345"), role="editor")
    db.create_user(s.db_path, username="view", email="v@ex.com", password_hash=hash_password("Abc12345"), role="viewer")
    Indexer(s).incremental(trigger="seed")
    app = create_app(s)
    with TestClient(app) as c:
        yield c, s


def _login(client, username, password="Abc12345"):
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def test_thumb_query_token_rbac(auth_app):
    client, s = auth_app
    admin_tok = _login(client, "admin")
    ed_tok = _login(client, "ed")
    view_tok = _login(client, "view")

    # header auth works for all roles (thumbs are readable)
    for tok in (admin_tok, ed_tok, view_tok):
        r = client.get("/api/thumb/1", headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200, r.text

    # query token fallback also works (covers <img src> legacy)
    for tok in (admin_tok, ed_tok, view_tok):
        r = client.get(f"/api/thumb/1?token={tok}")
        assert r.status_code == 200, f"query token failed for {tok[:8]}"

    # unauthenticated without token -> 401
    r = client.get("/api/thumb/1")
    assert r.status_code == 401

    # bad token -> 401/403
    r = client.get("/api/thumb/1?token=bad-token")
    assert r.status_code in (401, 403, 422)


def test_file_query_token_rbac(auth_app):
    client, s = auth_app
    view_tok = _login(client, "view")
    r = client.get(f"/api/file/1?token={view_tok}")
    assert r.status_code == 200
    r = client.get("/api/file/1")
    assert r.status_code == 401


def test_store_snapshot_run_admin_only(auth_app):
    client, s = auth_app
    admin_tok = _login(client, "admin")
    ed_tok = _login(client, "ed")
    view_tok = _login(client, "view")

    # viewer/editor must be forbidden
    for tok in (ed_tok, view_tok):
        r = client.post("/api/settings/store-snapshot/run", headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 403, r.text

    # admin succeeds (even if snapshot run does trivial work)
    r = client.post("/api/settings/store-snapshot/run", headers={"Authorization": f"Bearer {admin_tok}"})
    # May return 200 with result; allow 200
    assert r.status_code == 200, r.text
    assert "root_path" in r.json()


def test_store_snapshot_get_viewer_allowed(auth_app):
    client, s = auth_app
    view_tok = _login(client, "view")
    r = client.get("/api/settings/store-snapshot", headers={"Authorization": f"Bearer {view_tok}"})
    assert r.status_code == 200
    # unauthenticated -> 401
    r2 = client.get("/api/settings/store-snapshot")
    assert r2.status_code == 401


def test_delta_path_traversal_blocked(tmp_path, monkeypatch):
    """Crafted delta with ../../ must not escape store (indexer containment)."""
    monkeypatch.setattr(embeddings, "embed_images", _fake_embed)
    monkeypatch.setattr(metadata, "extract_xmp", lambda paths: {})
    store = tmp_path / "store"
    store.mkdir()
    data = tmp_path / "data"
    s = Settings(store_path=store, data_path=data, batch_size=8)
    s.ensure_dirs()
    # seed normal file
    Image.new("RGB", (8, 8), (1, 1, 1)).save(store / "ok.png")
    ix = Indexer(s)
    ix.incremental(trigger="seed")
    # create outside file that delta tries to reference
    outside = tmp_path / "evil.txt"
    outside.write_text("secret")
    # craft delta payload with traversal
    from backend.app.indexer import ScanReport
    rep = ScanReport(trigger="test", duration_sec=0, seen=0, processed=0, added=0, updated=0, removed=0, unchanged=0, failed=0)
    # direct call to containment helper
    assert ix._disk_file_from_live_stat("../../evil.txt", rep) is None
    assert rep.failed == 1
    assert ix._disk_file_from_live_stat("..\\..\\evil.txt", rep) is None
    assert ix._disk_file_from_live_stat("/absolute/path.png", rep) is None
    # normal still works
    rep2 = ScanReport(trigger="test", duration_sec=0, seen=0, processed=0, added=0, updated=0, removed=0, unchanged=0, failed=0)
    df = ix._disk_file_from_live_stat("ok.png", rep2)
    assert df is not None and df.rel_path == "ok.png"


def test_sanitize_filename_blocks_crlf(auth_app, monkeypatch):
    client, s = auth_app
    monkeypatch.setattr("backend.app.api.routes._store_file", lambda _s, _rel: s.store_path / "x.png")
    # Insert row with CRLF in rel_path name part (basename used for Content-Disposition)
    evil_name = 'evil\r\nSet-Cookie: a=b.png'
    evil_id = db.upsert_image(
        s.db_path, rel_path=evil_name, original_path=rf"\\nas\{evil_name}",
        size=1, mtime=1.0, sha256=None, width=None, height=None, xmp={},
    )
    admin_tok = _login(client, "admin")
    r = client.get(f"/api/file/{evil_id}", headers={"Authorization": f"Bearer {admin_tok}"})
    assert r.status_code == 200
    cd = r.headers.get("content-disposition", "")
    assert "\r" not in cd and "\n" not in cd
    # Header is `inline; filename="sanitized"; filename*=...` — delimiters have quotes, but sanitized name inside must have no CRLF
    assert "evil" in cd


def test_rate_limit_search_429(auth_app):
    client, s = auth_app
    tok = _login(client, "view")
    headers = {"Authorization": f"Bearer {tok}"}
    # 30/minute limit — 31st should 429 (allow some slack for other calls)
    statuses = []
    for _ in range(35):
        r = client.get("/api/stats", headers=headers)
        statuses.append(r.status_code)
    # at least one 429 expected once bucket exhausted
    assert 429 in statuses, f"expected 429 in {statuses[-5:]}"


def test_deleted_last_admin_blocked(auth_app):
    client, s = auth_app
    admin_tok = _login(client, "admin")
    # create second admin so we can delete one
    r = client.post("/api/users", headers={"Authorization": f"Bearer {admin_tok}"}, json={"username": "admin2", "email": "admin2@ex.com", "password": "Abc12345", "role": "admin"})
    assert r.status_code == 201
    admin2_id = r.json()["id"]
    # delete second admin ok
    r = client.delete(f"/api/users/{admin2_id}", headers={"Authorization": f"Bearer {admin_tok}"})
    assert r.status_code in (200, 204)
    # now only one admin left — delete should 400
    # need admin id
    users = client.get("/api/users", headers={"Authorization": f"Bearer {admin_tok}"}).json()["users"]
    last_admin_id = next(u["id"] for u in users if u["role"] == "admin")
    r = client.delete(f"/api/users/{last_admin_id}", headers={"Authorization": f"Bearer {admin_tok}"})
    assert r.status_code == 400
    assert "last admin" in r.json()["detail"].lower()
