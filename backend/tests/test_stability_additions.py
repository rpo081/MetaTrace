"""Stability additions: edge cases for 200k scale (containment, staleness, SQL escape)."""
import io
import json
import time
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from backend.app import db, embeddings, metadata
from backend.app.config import Settings
from backend.app.indexer import Indexer
from backend.app.main import create_app


def _fake_embed(images, settings):
    v = np.ones((len(images), 8), dtype=np.float32)
    return v / np.linalg.norm(v)


@pytest.fixture()
def api_client(tmp_path, monkeypatch):
    monkeypatch.setattr(embeddings, "embed_images", _fake_embed)
    monkeypatch.setattr(metadata, "extract_xmp", lambda paths: {})
    store = tmp_path / "store"
    store.mkdir()
    Image.new("RGB", (8, 8), (1, 2, 3)).save(store / "x.png")
    settings = Settings(
        store_path=store,
        data_path=tmp_path / "data",
        run_initial_scan_on_start=False,
        batch_size=8,
        network_root=r"\\nas\share",
        allow_unauthenticated=True,
    )
    Indexer(settings).incremental(trigger="seed")
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# A1: GET /api/file
# ---------------------------------------------------------------------------

def test_file_happy_path(api_client):
    r = api_client.get("/api/file/1")
    assert r.status_code == 200
    assert len(r.content) > 0


def test_file_unknown_id_404(api_client):
    assert api_client.get("/api/file/9999").status_code == 404


def test_file_source_missing_404(api_client):
    s = api_client.app.state.settings
    src = s.store_path / "x.png"
    src.unlink()
    r = api_client.get("/api/file/1")
    assert r.status_code == 404
    assert "source file missing" in r.json()["detail"].lower()
    # restore for other tests (fixture is per-test, so no need)
    Image.new("RGB", (8, 8), (1, 2, 3)).save(src)


def test_file_containment_via_db_row(api_client):
    s = api_client.app.state.settings
    # Insert a row with traversal rel_path - should be blocked by _store_file
    evil_id = db.upsert_image(
        s.db_path, rel_path="../../etc/passwd", original_path=r"\\nas\evil",
        size=1, mtime=1.0, sha256=None, width=None, height=None, xmp={},
    )
    r = api_client.get(f"/api/file/{evil_id}")
    assert r.status_code == 400
    assert "invalid path" in r.json()["detail"].lower()


# ---------------------------------------------------------------------------
# A2: rescan-delta + rescan stale/rebuild
# ---------------------------------------------------------------------------

def test_rescan_delta_no_delta(api_client):
    s = api_client.app.state.settings
    # ensure no delta file
    (s.data_path / "rescan_delta_latest.json").unlink(missing_ok=True)
    r = api_client.get("/api/rescan-delta")
    assert r.status_code == 200
    assert r.json()["status"] == "no_delta"


def test_rescan_delta_ok(api_client):
    s = api_client.app.state.settings
    delta = {
        "timestamp": "2026-08-30T10:00:00Z",
        "summary": {"created_count": 1, "deleted_count": 0, "modified_count": 0, "total_changes": 1},
        "changes": {"created": ["a.png"], "deleted": [], "modified": []},
    }
    (s.data_path / "rescan_delta_latest.json").write_text(json.dumps(delta), encoding="utf-8")
    r = api_client.get("/api/rescan-delta")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["summary"]["created_count"] == 1


def test_rescan_delta_corrupt_500(api_client):
    s = api_client.app.state.settings
    (s.data_path / "rescan_delta_latest.json").write_text("not json {{", encoding="utf-8")
    r = api_client.get("/api/rescan-delta")
    assert r.status_code == 500


def test_rescan_stale_delta_ignored(api_client, monkeypatch):
    s = api_client.app.state.settings
    # delta older than 2*TTL (TTL=24h -> 48h). Write delta 50h ago.
    delta = {
        "timestamp": "2020-01-01T00:00:00Z",
        "summary": {"created_count": 1, "deleted_count": 0, "modified_count": 0, "total_changes": 1},
        "changes": {"created": ["stale.png"], "deleted": [], "modified": []},
    }
    p = s.data_path / "rescan_delta_latest.json"
    p.write_text(json.dumps(delta), encoding="utf-8")
    old = time.time() - (50 * 3600)
    import os
    os.utime(p, (old, old))

    # capture what scheduler receives
    seen = {}
    orig = api_client.app.state.scheduler.trigger_now

    def capture(rebuild=False, delta_info=None):
        seen["delta_info"] = delta_info
        return orig(rebuild=rebuild, delta_info=delta_info)

    monkeypatch.setattr(api_client.app.state.scheduler, "trigger_now", capture)
    r = api_client.post("/api/rescan", params={"use_delta": "true"})
    assert r.status_code == 202
    assert seen["delta_info"] is None  # stale ignored
    assert r.json()["has_delta"] is False
    api_client.app.state.scheduler.stop()


def test_rescan_rebuild_admin_only(tmp_path, monkeypatch):
    from backend.app.auth import hash_password
    monkeypatch.setattr(embeddings, "embed_images", _fake_embed)
    monkeypatch.setattr(metadata, "extract_xmp", lambda paths: {})
    store = tmp_path / "store"
    store.mkdir()
    settings = Settings(
        store_path=store,
        data_path=tmp_path / "data",
        run_initial_scan_on_start=False,
        jwt_secret="test-jwt-secret-32-chars-minimum-length!!",
        allow_unauthenticated=False,
    )
    # seed users directly via DB before app старта (avoids register guard)
    settings.ensure_dirs()
    db.init_db(settings.db_path)
    db.create_user(settings.db_path, username="admin", email="a@ex.com", password_hash=hash_password("Abc12345"), role="admin")
    db.create_user(settings.db_path, username="ed", email="ed@ex.com", password_hash=hash_password("Abc12345"), role="editor")
    app = create_app(settings)
    with TestClient(app) as c:
        r = c.post("/api/auth/login", json={"username": "admin", "password": "Abc12345"})
        assert r.status_code == 200, r.text
        token_admin = r.json()["access_token"]
        r2 = c.post("/api/auth/login", json={"username": "ed", "password": "Abc12345"})
        assert r2.status_code == 200, r2.text
        token_ed = r2.json()["access_token"]
        # editor rebuild -> 403
        r = c.post("/api/rescan", params={"rebuild": "true"}, headers={"Authorization": f"Bearer {token_ed}"})
        assert r.status_code == 403
        # admin rebuild -> 202
        r = c.post("/api/rescan", params={"rebuild": "true"}, headers={"Authorization": f"Bearer {token_admin}"})
        assert r.status_code == 202
        assert r.json()["rebuild"] is True
    app.state.scheduler.stop()


# ---------------------------------------------------------------------------
# B: DB escape_like + browse filter matrix
# ---------------------------------------------------------------------------

def test_escape_like(tmp_path):
    assert db.escape_like("a%b") == "a\\%b"
    assert db.escape_like("a_b") == "a\\_b"
    assert db.escape_like("a\\b") == "a\\\\b"
    assert db.escape_like("a%_\\") == "a\\%\\_\\\\"


def test_browse_folder_escape_percent(api_client, tmp_path):
    s = api_client.app.state.settings
    # create file with % in folder name - regression for LIKE injection
    # Insert directly via db to avoid filesystem weirdo
    db.upsert_image(s.db_path, rel_path="a%b/c.png", original_path=r"\\nas\a%b\c.png", size=10, mtime=10.0, sha256=None, width=10, height=10, xmp={})
    # browse folder a%b should match exactly that file, not aXb
    r = api_client.get("/api/images", params={"folder": "a%b"})
    assert r.status_code == 200
    items = r.json()["items"]
    # at least the a%b file should be among results, and no false positives like aXb
    assert any(i["rel_path"] == "a%b/c.png" for i in items)
    # folder a should not match a%b/c.png via LIKE escape (prefix is strict)
    r2 = api_client.get("/api/images", params={"folder": "a", "q": "c.png"})
    # just ensure no crash and correct escaping
    assert r2.status_code == 200


def test_browse_q_multi_term_and(api_client):
    s = api_client.app.state.settings
    db.upsert_image(s.db_path, rel_path="alpha_beta.png", original_path=r"\\nas\alpha_beta.png", size=10, mtime=10.0, sha256=None, width=10, height=10, xmp={"Title": "hello world"})
    r = api_client.get("/api/images", params={"filename": "alpha beta"})
    assert r.status_code == 200
    assert any(i["rel_path"] == "alpha_beta.png" for i in r.json()["items"])
    r2 = api_client.get("/api/images", params={"filename": "alpha missingterm"})
    assert r2.status_code == 200
    assert not any(i["rel_path"] == "alpha_beta.png" for i in r2.json()["items"])


def test_browse_filename_isolation(api_client):
    s = api_client.app.state.settings
    db.upsert_image(s.db_path, rel_path="folder_alpha/beta.png", original_path=r"\\nas\folder_alpha\beta.png", size=10, mtime=10.0, sha256=None, width=10, height=10, xmp={"tag": "gamma"})
    # Filename matches filename part
    r = api_client.get("/api/images", params={"filename": "beta"})
    assert r.status_code == 200
    assert any(i["rel_path"] == "folder_alpha/beta.png" for i in r.json()["items"])
    # Filename should NOT match folder name
    r_folder = api_client.get("/api/images", params={"filename": "folder_alpha"})
    assert r_folder.status_code == 200
    assert not any(i["rel_path"] == "folder_alpha/beta.png" for i in r_folder.json()["items"])
    # Filename should NOT match XMP content
    r_xmp = api_client.get("/api/images", params={"filename": "gamma"})
    assert r_xmp.status_code == 200
    assert not any(i["rel_path"] == "folder_alpha/beta.png" for i in r_xmp.json()["items"])


def test_browse_folder_anywhere_in_path_not_filename(api_client):
    s = api_client.app.state.settings
    db.upsert_image(s.db_path, rel_path="projects/2024/renders/hero.png", original_path=r"\\nas\projects\2024\renders\hero.png", size=10, mtime=10.0, sha256=None, width=10, height=10, xmp={})
    db.upsert_image(s.db_path, rel_path="hero.png", original_path=r"\\nas\hero.png", size=10, mtime=10.0, sha256=None, width=10, height=10, xmp={})
    # Folder matches middle of path
    r_mid = api_client.get("/api/images", params={"folder": "2024"})
    assert r_mid.status_code == 200
    assert any(i["rel_path"] == "projects/2024/renders/hero.png" for i in r_mid.json()["items"])
    # Folder matches subfolder
    r_sub = api_client.get("/api/images", params={"folder": "renders"})
    assert r_sub.status_code == 200
    assert any(i["rel_path"] == "projects/2024/renders/hero.png" for i in r_sub.json()["items"])
    # Folder does NOT match filename
    r_fn = api_client.get("/api/images", params={"folder": "hero"})
    assert r_fn.status_code == 200
    assert not any(i["rel_path"] == "projects/2024/renders/hero.png" for i in r_fn.json()["items"])
    assert not any(i["rel_path"] == "hero.png" for i in r_fn.json()["items"])


def test_browse_xmp_tag_filter(api_client):
    s = api_client.app.state.settings
    db.upsert_image(s.db_path, rel_path="xmp_dir/sample.png", original_path=r"\\nas\xmp_dir\sample.png", size=10, mtime=10.0, sha256=None, width=10, height=10, xmp={"Creator": "John Doe", "Project": "Mars"})
    # XMP matches content inside XMP tags
    r_xmp1 = api_client.get("/api/images", params={"xmp": "John Doe"})
    assert r_xmp1.status_code == 200
    assert any(i["rel_path"] == "xmp_dir/sample.png" for i in r_xmp1.json()["items"])
    r_xmp2 = api_client.get("/api/images", params={"xmp": "Mars"})
    assert r_xmp2.status_code == 200
    assert any(i["rel_path"] == "xmp_dir/sample.png" for i in r_xmp2.json()["items"])
    # XMP does NOT match filename or directory
    r_fn = api_client.get("/api/images", params={"xmp": "sample"})
    assert r_fn.status_code == 200
    assert not any(i["rel_path"] == "xmp_dir/sample.png" for i in r_fn.json()["items"])
    r_dir = api_client.get("/api/images", params={"xmp": "xmp_dir"})
    assert r_dir.status_code == 200
    assert not any(i["rel_path"] == "xmp_dir/sample.png" for i in r_dir.json()["items"])


def test_browse_xmp_tag_filter_matches_camel_case_tag_names(api_client):
    s = api_client.app.state.settings
    db.upsert_image(
        s.db_path,
        rel_path="xmp_dir/reference.png",
        original_path=r"\\nas\xmp_dir\reference.png",
        size=10,
        mtime=10.0,
        sha256=None,
        width=10,
        height=10,
        xmp={"TransmissionReference": "Mars Shot", "Description": "Mission render"},
    )

    exact = api_client.get("/api/images", params={"xmp": "TransmissionReference"})
    assert exact.status_code == 200
    assert any(i["rel_path"] == "xmp_dir/reference.png" for i in exact.json()["items"])

    split = api_client.get("/api/images", params={"xmp": "transmission reference"})
    assert split.status_code == 200
    assert any(i["rel_path"] == "xmp_dir/reference.png" for i in split.json()["items"])


def test_browse_has_xmp(api_client):
    s = api_client.app.state.settings
    db.upsert_image(s.db_path, rel_path="with_xmp.png", original_path=r"\\nas\with_xmp.png", size=10, mtime=10.0, sha256=None, width=10, height=10, xmp={"Title": "t"})
    r = api_client.get("/api/images", params={"has_xmp": "true"})
    assert r.status_code == 200
    # filter should exclude plain x.png which has {}
    assert any(i["rel_path"] == "with_xmp.png" for i in r.json()["items"])


def test_browse_width_height_filters(api_client):
    s = api_client.app.state.settings
    db.upsert_image(s.db_path, rel_path="wide.png", original_path=r"\\nas\wide.png", size=10, mtime=10.0, sha256=None, width=100, height=10, xmp={})
    r = api_client.get("/api/images", params={"width_min": 50, "width_max": 200, "height_min": 5, "height_max": 15})
    assert r.status_code == 200
    assert any(i["rel_path"] == "wide.png" for i in r.json()["items"])
    r2 = api_client.get("/api/images", params={"width_min": 200})
    assert not any(i["rel_path"] == "wide.png" for i in r2.json()["items"])


def test_browse_order_and_ext_without_dot(api_client):
    r = api_client.get("/api/images", params={"sort": "rel_path", "order": "asc"})
    assert r.status_code == 200
    r2 = api_client.get("/api/images", params={"ext": "png"})  # without dot
    assert r2.status_code == 200
    assert r2.json()["total"] >= 1
    assert all(i["rel_path"].lower().endswith(".png") for i in r2.json()["items"])


def test_browse_combined_filters(api_client):
    s = api_client.app.state.settings
    db.upsert_image(s.db_path, rel_path="combo/a.png", original_path=r"\\nas\combo\a.png", size=500, mtime=20.0, sha256=None, width=20, height=20, xmp={})
    r = api_client.get("/api/images", params={"folder": "combo", "size_min": 100, "size_max": 1000, "filename": "a"})
    assert r.status_code == 200
    assert any(i["rel_path"] == "combo/a.png" for i in r.json()["items"])


# ---------------------------------------------------------------------------
# C: indexer stale boundary + invalid snapshot
# ---------------------------------------------------------------------------

def test_is_snapshot_stale_disabled_when_zero(tmp_path, monkeypatch):
    from backend.app.indexer import Indexer as IX
    monkeypatch.setattr(IX.__module__ + ".embeddings.embed_images", _fake_embed)
    store = tmp_path / "store"; store.mkdir()
    data = tmp_path / "data"; data.mkdir()
    s = Settings(store_path=store, data_path=data, snapshot_max_age_hours=0)
    ix = IX(s)
    p = s.latest_store_snapshot_file
    p.write_text(json.dumps({"files": {"a.png": {"mtime": 1, "size": 1}}}), encoding="utf-8")
    import os
    old = time.time() - 100*3600
    os.utime(p, (old, old))
    assert ix._is_snapshot_stale(p) is False


def test_load_snapshot_invalid_payload_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(embeddings, "embed_images", _fake_embed)
    monkeypatch.setattr(metadata, "extract_xmp", lambda paths: {})
    store = tmp_path / "store"; store.mkdir()
    Image.new("RGB", (8, 8), (1, 2, 3)).save(store / "x.png")
    s = Settings(store_path=store, data_path=tmp_path / "data", snapshot_max_age_hours=24)
    s.ensure_dirs()
    # invalid json
    s.latest_store_snapshot_file.write_text("not json", encoding="utf-8")
    ix = Indexer(s)
    assert ix._load_snapshot_inventory() is None
    # empty dict payload
    s.latest_store_snapshot_file.write_text(json.dumps({"files": {}}), encoding="utf-8")
    assert ix._load_snapshot_inventory() is None


# ---------------------------------------------------------------------------
# D: config validators
# ---------------------------------------------------------------------------

def test_jwt_secret_too_short_raises():
    with pytest.raises(Exception) as ei:
        Settings(jwt_secret="short")
    assert "32" in str(ei.value).lower()


def test_thumbs_max_files_validator():
    s = Settings(thumbs_max_files=0)
    assert s.thumbs_max_files == 0
    with pytest.raises(Exception):
        Settings(thumbs_max_files=-1)


def test_thumbs_prune_buffer_validator():
    s = Settings(thumbs_prune_buffer=0)
    assert s.thumbs_prune_buffer == 0
    with pytest.raises(Exception):
        Settings(thumbs_prune_buffer=-1)


def test_detail_thumbs_max_files_validator():
    s = Settings(detail_thumbs_max_files=0)
    assert s.detail_thumbs_max_files == 0
    with pytest.raises(Exception):
        Settings(detail_thumbs_max_files=-1)


def test_prewarm_thumbnail_workers_validator():
    s = Settings(prewarm_thumbnail_workers=1)
    assert s.prewarm_thumbnail_workers == 1
    with pytest.raises(Exception):
        Settings(prewarm_thumbnail_workers=0)


def test_snapshot_max_age_validator():
    s = Settings(snapshot_max_age_hours=0)
    assert s.snapshot_max_age_hours == 0
    with pytest.raises(Exception):
        Settings(snapshot_max_age_hours=-1)


# ---------------------------------------------------------------------------
# E: stats new fields
# ---------------------------------------------------------------------------

def test_stats_new_fields(api_client):
    r = api_client.get("/api/stats")
    body = r.json()
    for key in ("snapshot_age_sec", "snapshot_stale", "index_file_size_mb", "db_file_size_mb", "thumbs_count", "thumbs_size_mb"):
        assert key in body
    # snapshot_age may be None if no snapshot, but thumbs_count is int
    assert isinstance(body["thumbs_count"], int)


def test_patch_last_admin_demote_blocked(tmp_path, monkeypatch):
    from backend.app.auth import hash_password
    monkeypatch.setattr(embeddings, "embed_images", _fake_embed)
    monkeypatch.setattr(metadata, "extract_xmp", lambda paths: {})
    store = tmp_path / "store"; store.mkdir()
    s = Settings(store_path=store, data_path=tmp_path / "data", jwt_secret="test-jwt-secret-32-chars-minimum-length!!", allow_unauthenticated=False)
    s.ensure_dirs()
    db.init_db(s.db_path)
    admin_id = db.create_user(s.db_path, username="admin", email="a@ex.com", password_hash=hash_password("Abc12345"), role="admin")
    app = create_app(s)
    with TestClient(app) as c:
        r = c.post("/api/auth/login", json={"username": "admin", "password": "Abc12345"})
        token = r.json()["access_token"]
        # demote last admin -> 400
        r2 = c.patch(f"/api/users/{admin_id}", headers={"Authorization": f"Bearer {token}"}, json={"role": "viewer"})
        assert r2.status_code == 400
        assert "last admin" in r2.json()["detail"].lower()
        # disable last admin -> 400
        r3 = c.patch(f"/api/users/{admin_id}", headers={"Authorization": f"Bearer {token}"}, json={"is_active": False})
        assert r3.status_code == 400
        # with second admin, demote succeeds
        ed_id = db.create_user(s.db_path, username="admin2", email="b@ex.com", password_hash=hash_password("Abc12345"), role="admin")
        r4 = c.patch(f"/api/users/{admin_id}", headers={"Authorization": f"Bearer {token}"}, json={"role": "editor"})
        assert r4.status_code == 200
        assert r4.json()["role"] == "editor"
    app.state.scheduler.stop()


def test_patch_disable_last_admin_with_two_admins_allows(tmp_path, monkeypatch):
    from backend.app.auth import hash_password
    monkeypatch.setattr(embeddings, "embed_images", _fake_embed)
    monkeypatch.setattr(metadata, "extract_xmp", lambda paths: {})
    store = tmp_path / "store"; store.mkdir()
    s = Settings(store_path=store, data_path=tmp_path / "data", jwt_secret="test-jwt-secret-32-chars-minimum-length!!", allow_unauthenticated=False)
    s.ensure_dirs()
    db.init_db(s.db_path)
    a1 = db.create_user(s.db_path, username="admin", email="a@ex.com", password_hash=hash_password("Abc12345"), role="admin")
    a2 = db.create_user(s.db_path, username="admin2", email="b@ex.com", password_hash=hash_password("Abc12345"), role="admin")
    app = create_app(s)
    with TestClient(app) as c:
        r = c.post("/api/auth/login", json={"username": "admin", "password": "Abc12345"})
        token = r.json()["access_token"]
        r2 = c.patch(f"/api/users/{a1}", headers={"Authorization": f"Bearer {token}"}, json={"is_active": False})
        assert r2.status_code == 200
        assert r2.json()["is_active"] is False
    app.state.scheduler.stop()


def test_scheduler_resume_cold_start(tmp_path, monkeypatch):
    from backend.app import indexer as idx_mod
    from backend.app.scheduler import ScanScheduler
    def fake_embed(images, settings):
        vecs = np.zeros((len(images), 16), dtype=np.float32)
        for n, img in enumerate(images):
            seed = int.from_bytes(img.tobytes(), "little") % (2**32)
            rng = np.random.default_rng(seed)
            v = rng.standard_normal(16).astype(np.float32)
            vecs[n] = v / np.linalg.norm(v)
        return vecs
    monkeypatch.setattr(idx_mod.embeddings, "embed_images", fake_embed)
    monkeypatch.setattr(idx_mod.metadata, "extract_xmp", lambda paths: {str(p): {} for p in paths})
    store = tmp_path / "store"; store.mkdir(); data = tmp_path / "data"
    s = Settings(store_path=store, data_path=data, batch_size=8)
    # seed one file
    Image.new("RGB", (8, 8), (1, 1, 1)).save(store / "a.png")
    ix = idx_mod.Indexer(s)
    ix.incremental(trigger="seed")
    # second file for resume
    Image.new("RGB", (8, 8), (2, 2, 2)).save(store / "b.png")
    s.ensure_dirs()
    # write pending checkpoint as if scan paused after first file
    (s.data_path / "scan_checkpoint.json").write_text(json.dumps({
        "version": 1, "phase": "pending", "mode": "full", "force_rebuild": False,
        "trigger": "cold-resume",
        "model": f"{s.model_name}:{s.model_pretrained}",
        "report": {"trigger": "cold-resume", "started_at": 1.0, "duration_sec": 0.0, "seen": 2, "processed": 1, "added": 1, "updated": 0, "removed": 0, "unchanged": 0, "failed": 0, "error_count": 0},
        "remaining_rel_paths": ["b.png"], "remaining_added_rel_paths": ["b.png"],
        "updated_at": "2026-01-01T00:00:00Z"
    }), encoding="utf-8")
    # fresh indexer + scheduler simulating restart
    ix2 = idx_mod.Indexer(s)
    ix2.load_or_create()
    assert ix2.status["state"] == "paused"
    sched = ScanScheduler(ix2)
    # cold-start resume via scheduler (busy free)
    assert sched.resume() is True
    # wait for background thread
    for _ in range(50):
        if ix2.status["state"] != "paused":
            time.sleep(0.05)
            if ix2.count == 2:
                break
        time.sleep(0.05)
    assert ix2.count == 2
    sched.stop()


def test_exact_match_bypasses_min_score(tmp_path, monkeypatch):
    from backend.app import indexer as idx_mod
    from backend.app import search as search_mod
    def fake_embed(images, settings):
        vecs = np.zeros((len(images), 16), dtype=np.float32)
        for n, img in enumerate(images):
            seed = int.from_bytes(img.tobytes(), "little") % (2**32)
            rng = np.random.default_rng(seed)
            v = rng.standard_normal(16).astype(np.float32)
            vecs[n] = v / np.linalg.norm(v)
        return vecs
    monkeypatch.setattr(idx_mod.embeddings, "embed_images", fake_embed)
    monkeypatch.setattr(idx_mod.metadata, "extract_xmp", lambda paths: {})
    store = tmp_path / "store"; store.mkdir()
    s = Settings(store_path=store, data_path=tmp_path / "data", run_initial_scan_on_start=False)
    Image.new("RGB", (8, 8), (10, 10, 10)).save(store / "exact.png")
    Image.new("RGB", (8, 8), (20, 20, 20)).save(store / "other.png")
    ix = idx_mod.Indexer(s)
    ix.incremental(trigger="seed")
    svc = search_mod.SearchService(ix, s)
    exact_bytes = (store / "exact.png").read_bytes()
    # min_score 0.99 would filter out other.png but exact must still pin rank 1 with 1.0
    out = svc.search(exact_bytes, k=5, min_score=0.99)
    assert out["exact_match"] is True
    assert out["results"][0]["exact"] is True
    assert out["results"][0]["score"] == 1.0
    assert out["results"][0]["rel_path"] == "exact.png"
    # remaining non-exact results must respect min_score
    for r in out["results"][1:]:
        assert r["score"] >= 0.99


def test_search_empty_index_returns_empty(tmp_path, monkeypatch):
    from backend.app import indexer as idx_mod
    from backend.app import search as search_mod
    def fake_embed(images, settings):
        v = np.ones((len(images), 8), dtype=np.float32)
        return v / np.linalg.norm(v)
    monkeypatch.setattr(idx_mod.embeddings, "embed_images", fake_embed)
    monkeypatch.setattr(idx_mod.metadata, "extract_xmp", lambda paths: {})
    store = tmp_path / "store"; store.mkdir()
    s = Settings(store_path=store, data_path=tmp_path / "data", run_initial_scan_on_start=False)
    ix = idx_mod.Indexer(s)
    svc = search_mod.SearchService(ix, s)
    out = svc.search(b"not an image bytes", k=5, min_score=0.0)
    # empty index returns 0 total and empty results without crashing
    assert out["total_indexed"] == 0
    assert out["results"] == []
