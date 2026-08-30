"""API tests with a temp store; no CLIP model needed for these endpoints."""
import io
import json
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from backend.app import embeddings, metadata
from backend.app import store_snapshot
from backend.app.config import Settings
from backend.app.indexer import Indexer
from backend.app.main import create_app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    def fake_embed(images, settings):
        v = np.ones((len(images), 8), dtype=np.float32)
        return v / np.linalg.norm(v)

    monkeypatch.setattr(embeddings, "embed_images", fake_embed)
    monkeypatch.setattr(metadata, "extract_xmp", lambda paths: {})

    store = tmp_path / "store"
    store.mkdir()
    img = Image.new("RGB", (8, 8), (1, 2, 3))
    img.save(store / "x.png")
    settings = Settings(
        store_path=store,
        data_path=tmp_path / "data",
        run_initial_scan_on_start=False,
        batch_size=8,
        network_root=r"\\nas\share",
        allow_unauthenticated=True,
    )
    Indexer(settings).incremental(trigger="seed")  # ensure non-empty index on disk

    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json() == {"ok": True}


def test_metatrace_env_vars_map_to_settings(monkeypatch):
    """METATRACE_* names must reach the prefixed-off settings model."""
    from backend.app.config import Settings as S

    monkeypatch.setenv("LOCAL_IMAGE_STORE", "Z:/images")
    monkeypatch.setenv("METATRACE_ADMIN_TOKEN", "abc")
    monkeypatch.setenv("METATRACE_CORS_ORIGINS", "http://a.example, http://b.example")
    s = S()
    assert s.store_path == Path("Z:/images")
    assert s.admin_token == "abc"
    assert s.cors_origin_list == ["http://a.example", "http://b.example"]
    monkeypatch.delenv("LOCAL_IMAGE_STORE")
    monkeypatch.delenv("METATRACE_ADMIN_TOKEN")
    monkeypatch.delenv("METATRACE_CORS_ORIGINS")
    s2 = S()
    assert s2.admin_token is None and s2.cors_origins is None


def test_stats_sanitized_and_parity(client):
    """No absolute paths leak; db_count/max_upload_mb exposed for parity."""
    client.app.state.settings.latest_store_snapshot_file.write_text(
        json.dumps(
            {
                "version": 1,
                "files": {
                    "x.png": {"mtime": 1.0, "size": 1},
                    "y.jpg": {"mtime": 1.0, "size": 1},
                    "notes.txt": {"mtime": 1.0, "size": 1},
                },
            }
        ),
        encoding="utf-8",
    )
    r = client.get("/api/stats")
    body = r.json()
    assert r.status_code == 200
    for key in ("indexed", "db_count", "state", "last_report", "last_scan",
                "model", "exiftool", "max_upload_mb", "snapshot_image_count"):
        assert key in body
    # sanitized: no store/network topology disclosure
    assert "store_path" not in body
    assert "network_root" not in body
    assert str(client.app.state.settings.store_path) not in r.text
    # parity observability
    assert body["indexed"] == body["db_count"] == 1
    assert body["snapshot_image_count"] == 2
    assert body["max_upload_mb"] == client.app.state.settings.max_upload_mb
    assert body["inventory_source"] is None


def test_last_report_has_error_count_only(client):
    st = client.app.state.indexer.status
    if st["last_report"]:
        assert "errors" not in st["last_report"]
        assert "error_count" in st["last_report"]


def test_security_headers_present(client):
    r = client.get("/api/health")
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    csp = r.headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp
    assert "img-src 'self' blob:" in csp
    assert "frame-ancestors 'none'" in csp
    assert r.headers["Referrer-Policy"] == "no-referrer"


def test_cors_disabled_by_default(client):
    r = client.get("/api/health", headers={"Origin": "http://evil.example"})
    assert "access-control-allow-origin" not in {k.lower() for k in r.headers}


def test_cors_enabled_when_configured(tmp_path, monkeypatch):
    def fake_embed(images, settings):
        v = np.ones((len(images), 8), dtype=np.float32)
        return v / np.linalg.norm(v)

    monkeypatch.setattr(embeddings, "embed_images", fake_embed)
    monkeypatch.setattr(metadata, "extract_xmp", lambda paths: {})
    store = tmp_path / "store"
    store.mkdir()
    settings = Settings(
        store_path=store,
        data_path=tmp_path / "data",
        run_initial_scan_on_start=False,
        cors_origins="http://localhost:5173, https://ui.example.com",
    )
    app = create_app(settings)
    with TestClient(app) as c:
        r = c.get("/api/health", headers={"Origin": "http://localhost:5173"})
        assert r.headers["access-control-allow-origin"] == "http://localhost:5173"
        r2 = c.get("/api/health", headers={"Origin": "http://evil.example"})
        assert "access-control-allow-origin" not in {k.lower() for k in r2.headers}


def test_search_requires_file_or_query(client):
    r = client.post("/api/search")
    assert r.status_code in (400, 422)


def test_search_by_text_query(client):
    r = client.post("/api/search", params={"q": "image"})
    assert r.status_code == 200
    assert "results" in r.json()


def test_search_rejects_invalid_combine_mode(client):
    r = client.post("/api/search", params={"q": "image", "combine": "xor"})
    assert r.status_code == 400
    assert r.json()["detail"] == "combine must be 'and' or 'or'"


def test_search_accepts_or_mode(client):
    r = client.post("/api/search", params={"q": "image", "combine": "or"})
    assert r.status_code == 200
    assert "results" in r.json()


def test_store_snapshot_settings_defaults_to_install_path(client):
    r = client.get("/api/settings/store-snapshot")
    assert r.status_code == 200
    body = r.json()
    assert body["root_path"] == str(client.app.state.settings.store_path)
    assert body["default_root_path"] == str(client.app.state.settings.store_path)
    assert body["configured_root_path"] is None
    assert body["uses_default"] is True
    assert body["source"] == "store_path"


def test_store_snapshot_settings_report_env_root(tmp_path, monkeypatch):
    app, settings = _minimal_app(
        tmp_path,
        _monkeypatch=monkeypatch,
        snapshot_scan_root=tmp_path / "share-root",
    )
    settings.snapshot_scan_root.mkdir(parents=True, exist_ok=True)
    with TestClient(app) as c:
        r = c.get("/api/settings/store-snapshot")
        assert r.status_code == 200
        body = r.json()
        assert body["root_path"] == str(settings.snapshot_scan_root)
        assert body["configured_root_path"] == str(settings.snapshot_scan_root)
        assert body["uses_default"] is False
        assert body["source"] == "env"
    app.state.scheduler.stop()


def test_store_snapshot_run_uses_env_path(client, monkeypatch):
    seen = {}

    from backend.app.api import routes as routes_mod

    def fake_detect_changes(*, root_path, snapshot_file, data_folder, allowed_extensions=None, on_progress=None):
        seen["root_path"] = root_path
        seen["snapshot_file"] = snapshot_file
        seen["data_folder"] = data_folder
        seen["allowed_extensions"] = allowed_extensions
        return {
            "root_path": str(root_path),
            "duration_sec": 0.12,
            "initialized": False,
            "summary": {
                "created_count": 1,
                "deleted_count": 0,
                "modified_count": 2,
                "total_changes": 3,
            },
            "changes": {"created": ["a.png"], "deleted": [], "modified": ["b.png", "c.png"]},
            "delta_file": "rescan_delta_latest.json",
        }

    monkeypatch.setattr(routes_mod.store_snapshot, "detect_changes", fake_detect_changes)
    r = client.post("/api/settings/store-snapshot/run")
    assert r.status_code == 200
    assert r.json()["summary"]["total_changes"] == 3
    assert seen["root_path"] == str(client.app.state.settings.default_snapshot_scan_root)
    assert seen["snapshot_file"] == client.app.state.settings.baseline_snapshot_file
    assert seen["data_folder"] == client.app.state.settings.data_path
    assert seen["allowed_extensions"] == client.app.state.settings.extensions


def test_store_snapshot_ignores_non_image_files(tmp_path):
    root = tmp_path / "share"
    root.mkdir()
    (root / "render.png").write_bytes(b"png")
    (root / "notes.txt").write_text("ignore me", encoding="utf-8")

    snapshot_file = tmp_path / "data" / "store_snapshot.json"
    result = store_snapshot.detect_changes(
        root_path=root,
        snapshot_file=snapshot_file,
        data_folder=tmp_path / "data",
    )

    assert result["initialized"] is True
    payload = json.loads(snapshot_file.read_text(encoding="utf-8"))
    assert payload["file_count"] == 1
    assert set(payload["files"]) == {"render.png"}


def test_search_rejects_undecodable_image(client):
    r = client.post(
        "/api/search",
        files={"file": ("q.png", b"not an image", "image/png")},
        data={"k": "5"},
    )
    assert r.status_code == 400
    # sanitized error detail
    assert r.json()["detail"] == "could not decode image"


def test_upload_limit_413_streamed_abort(client, tmp_path):
    """Oversized uploads must 413 without reading the whole body first."""
    s = client.app.state.settings
    big = s.max_upload_mb + 8
    payload = b"\x00" * (big * 1024 * 1024)  # > limit; decode would fail anyway
    r = client.post(
        "/api/search",
        files={"file": ("q.png", payload, "image/png")},
    )
    assert r.status_code == 413


def test_thumb_unknown_id_404(client):
    assert client.get("/api/thumb/9999").status_code == 404


def test_thumb_size_clamped(client):
    # sizes outside 64..1024 are clamped; cache file name reflects the clamp
    r = client.get("/api/thumb/1", params={"size": 999999})
    assert r.status_code == 200
    thumbs = sorted(p.name for p in client.app.state.settings.thumbs_dir.glob("*.png"))
    assert thumbs == ["1_1024.png"]
    r = client.get("/api/thumb/1", params={"size": 1})
    assert r.status_code == 200
    thumbs = sorted(p.name for p in client.app.state.settings.thumbs_dir.glob("*.png"))
    assert thumbs == ["1_1024.png", "1_64.png"]


def test_thumb_preserves_png_alpha(client):
    source = client.app.state.settings.store_path / "x.png"
    Image.new("RGBA", (8, 8), (255, 0, 0, 0)).save(source)

    response = client.get("/api/thumb/1")
    assert response.status_code == 200
    thumbs = sorted(p.name for p in client.app.state.settings.thumbs_dir.glob("*.png"))
    assert thumbs == ["1_256.png"]
    with Image.open(io.BytesIO(response.content)) as thumbnail:
        assert thumbnail.mode == "RGBA"
        assert thumbnail.getpixel((0, 0))[3] == 0


def test_file_and_thumb_containment_against_symlink_escape(client, tmp_path):
    """A symlink planted in the store must not serve files outside it."""
    s = client.app.state.settings
    secret = tmp_path / "secret.png"
    Image.new("RGB", (4, 4)).save(secret)
    link = s.store_path / "evil.png"
    try:
        link.symlink_to(secret)
    except OSError:
        pytest.skip("symlinks unavailable on this platform")
    from backend.app.api.routes import _store_file
    with pytest.raises(Exception) as ei:
        _store_file(s, "evil.png")
    assert getattr(ei.value, "status_code", None) == 400


def test_store_file_rejects_absolute_traversal(client):
    from backend.app.api.routes import _store_file
    s = client.app.state.settings
    with pytest.raises(Exception) as ei:
        _store_file(s, "../../etc/passwd")
    assert getattr(ei.value, "status_code", None) == 400


def test_rescan_conflict_while_running(client, monkeypatch):
    held = client.app.state.scheduler._busy  # hold the lock to simulate a running scan
    assert held.acquire(blocking=False)
    try:
        r = client.post("/api/rescan")
        assert r.status_code == 409
    finally:
        held.release()


def test_rescan_admin_token_enforced(tmp_path, monkeypatch):
    def fake_embed(images, settings):
        v = np.ones((len(images), 8), dtype=np.float32)
        return v / np.linalg.norm(v)

    monkeypatch.setattr(embeddings, "embed_images", fake_embed)
    monkeypatch.setattr(metadata, "extract_xmp", lambda paths: {})
    store = tmp_path / "store"
    store.mkdir()
    settings = Settings(
        store_path=store,
        data_path=tmp_path / "data",
        run_initial_scan_on_start=False,
        admin_token="s3cret",
    )
    app = create_app(settings)
    with TestClient(app) as c:
        # missing header -> 403
        assert c.post("/api/rescan").status_code == 403
        # wrong token -> 403
        assert c.post("/api/rescan", headers={"X-Admin-Token": "nope"}).status_code == 403
        # non-ASCII token -> 403, not TypeError/500 (L1 regression). httpx only
        # accepts raw bytes for non-ASCII header values; Starlette decodes them
        # latin-1 server-side, reproducing the crafted-request scenario.
        r = c.post(
            "/api/rescan",
            headers={"X-Admin-Token": "s3crét-€".encode("utf-8")},
        )
        assert r.status_code == 403
        # correct token -> accepted
        ok = c.post("/api/rescan", headers={"X-Admin-Token": "s3cret"})
        assert ok.status_code == 202
    app.state.scheduler.stop()


def test_rescan_allowed_without_token_but_warns_once(client, caplog, monkeypatch):
    """Rescan works in trusted-LAN mode; the admin-token warning fires at
    startup (once during lifespan), not per-request.  Verify the endpoint
    still accepts the request (202).
    """
    with caplog.at_level("WARNING"):
        r = client.post("/api/rescan")
    assert r.status_code == 202
    client.app.state.scheduler.stop()


def test_rescan_pause_resume_routes(client, monkeypatch):
    pause_calls = []
    resume_calls = []

    monkeypatch.setattr(client.app.state.scheduler, "pause", lambda: pause_calls.append(True) or True)
    monkeypatch.setattr(client.app.state.scheduler, "resume", lambda: resume_calls.append(True) or True)

    paused = client.post("/api/rescan/pause")
    resumed = client.post("/api/rescan/resume")

    assert paused.status_code == 202
    assert resumed.status_code == 202
    assert pause_calls == [True]
    assert resume_calls == [True]


def test_rescan_pause_resume_conflicts(client, monkeypatch):
    monkeypatch.setattr(client.app.state.scheduler, "pause", lambda: False)
    monkeypatch.setattr(client.app.state.scheduler, "resume", lambda: False)

    paused = client.post("/api/rescan/pause")
    resumed = client.post("/api/rescan/resume")

    assert paused.status_code == 409
    assert resumed.status_code == 409


def test_startup_exposes_paused_state_from_resume_checkpoint(tmp_path, monkeypatch):
    app, settings = _minimal_app(tmp_path, _monkeypatch=monkeypatch)
    settings.ensure_dirs()
    (settings.store_path / "x.png").write_bytes(b"not used")
    checkpoint = settings.data_path / "scan_checkpoint.json"
    checkpoint.write_text(
        '{"version":1,"phase":"pending","mode":"full","force_rebuild":false,'
        '"trigger":"resume-test","model":"ViT-B-32-quickgelu:openai",'
        '"report":{"trigger":"resume-test","started_at":1.0,"duration_sec":0.0,'
        '"seen":10,"processed":4,"added":4,"updated":0,"removed":0,'
        '"unchanged":0,"failed":0,"error_count":0},'
        '"remaining_rel_paths":["later.png"],"remaining_added_rel_paths":["later.png"],'
        '"updated_at":"2026-01-01T00:00:00Z"}',
        encoding="utf-8",
    )
    with TestClient(app) as c:
        stats = c.get("/api/stats")
        assert stats.status_code == 200
        body = stats.json()
        assert body["state"] == "paused"
        assert body["last_report"]["processed"] == 4
    app.state.scheduler.stop()


def _minimal_app(tmp_path, **overrides):
    def fake_embed(images, settings):
        v = np.ones((len(images), 8), dtype=np.float32)
        return v / np.linalg.norm(v)

    monkeypatch_attrs = overrides.pop("_monkeypatch")
    monkeypatch_attrs.setattr(embeddings, "embed_images", fake_embed)
    monkeypatch_attrs.setattr(metadata, "extract_xmp", lambda paths: {})
    store = tmp_path / "store"
    store.mkdir()
    settings = Settings(
        store_path=store,
        data_path=tmp_path / "data",
        run_initial_scan_on_start=False,
        allow_unauthenticated=True,
        **overrides,
    )
    return create_app(settings), settings


def test_startup_warns_when_admin_token_unset(tmp_path, monkeypatch, caplog):
    """K5 residual: trusted-LAN mode must be announced loudly at startup."""
    app, _ = _minimal_app(tmp_path, _monkeypatch=monkeypatch, admin_token=None)
    with caplog.at_level("WARNING"):
        with TestClient(app):
            pass
    assert any(
        "trusted-LAN" in rec.getMessage() for rec in caplog.records
    ), "startup must warn when METATRACE_ADMIN_TOKEN is unset"
    app.state.scheduler.stop()


def test_startup_quiet_when_admin_token_set(tmp_path, monkeypatch, caplog):
    app, _ = _minimal_app(tmp_path, _monkeypatch=monkeypatch, admin_token="t0k3n")
    with caplog.at_level("WARNING"):
        with TestClient(app):
            pass
    assert not any("trusted-LAN" in rec.getMessage() for rec in caplog.records)
    app.state.scheduler.stop()


def test_startup_cleans_orphaned_thumb_temps(tmp_path, monkeypatch):
    """I3: crashed thumbnail writes leave temp files; startup sweeps them."""
    app, settings = _minimal_app(tmp_path, _monkeypatch=monkeypatch)
    settings.ensure_dirs()
    keep = settings.thumbs_dir / "1_512.png"
    keep.write_bytes(b"png-bytes")            # real cache entry: untouched
    orphans = [
        settings.thumbs_dir / "tmpAbCdEf.tmp",  # mkstemp(suffix=".tmp") shape
        settings.thumbs_dir / ".tmphidden",     # literal .tmp* shape
    ]
    for p in orphans:
        p.write_bytes(b"partial")
    with TestClient(app):
        pass
    assert keep.exists()
    assert not any(p.exists() for p in orphans)
    app.state.scheduler.stop()


# ---------- browse images ----------


def test_browse_images_default(client):
    """GET /api/images returns results with default params."""
    r = client.get("/api/images")
    assert r.status_code == 200
    body = r.json()
    assert "items" in body
    assert "total" in body
    assert "offset" in body
    assert "limit" in body
    assert "has_more" in body
    assert body["total"] >= 1
    assert len(body["items"]) >= 1
    item = body["items"][0]
    for key in ("id", "rel_path", "original_path", "width", "height",
                "xmp", "size", "mtime", "sha256", "indexed_at",
                "thumb_url", "file_url"):
        assert key in item
    assert item["thumb_url"].startswith("/api/thumb/")
    assert item["file_url"].startswith("/api/file/")


def test_browse_images_pagination(client):
    """offset and limit control pagination."""
    r_all = client.get("/api/images", params={"offset": 0, "limit": 100})
    total = r_all.json()["total"]
    assert total >= 1

    r_p1 = client.get("/api/images", params={"offset": 0, "limit": 1})
    assert r_p1.status_code == 200
    body_p1 = r_p1.json()
    assert len(body_p1["items"]) == 1
    assert body_p1["total"] == total
    assert body_p1["offset"] == 0
    assert body_p1["has_more"] is (total > 1)

    # When requesting beyond total, should get empty items
    r_past = client.get("/api/images", params={"offset": total, "limit": 10})
    assert r_past.status_code == 200
    body_past = r_past.json()
    assert len(body_past["items"]) == 0
    assert body_past["has_more"] is False


def test_browse_images_filters_ext(client):
    """ext filter restricts results to matching extension."""
    r = client.get("/api/images", params={"ext": ".png"})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] >= 1
    for item in body["items"]:
        assert item["rel_path"].lower().endswith(".png")

    r_none = client.get("/api/images", params={"ext": ".xyz"})
    assert r_none.status_code == 200
    assert r_none.json()["total"] == 0


def test_browse_images_filters_size(client):
    """size_min/size_max filter correctly constrains results."""
    r_min = client.get("/api/images", params={"size_min": 0})
    assert r_min.status_code == 200
    assert r_min.json()["total"] >= 1

    r_big = client.get("/api/images", params={"size_min": 999999999})
    assert r_big.status_code == 200
    assert r_big.json()["total"] == 0


def test_browse_images_filters_folder(client):
    """folder filter uses LIKE prefix match."""
    # Get a known rel_path prefix
    r_all = client.get("/api/images", params={"limit": 1})
    assert r_all.status_code == 200
    items = r_all.json()["items"]
    if items:
        # Use the filename part (no folder) — should still match
        rel = items[0]["rel_path"]
        folder = rel.rsplit("/", 1)[0] if "/" in rel else ""
        if folder:
            r = client.get("/api/images", params={"folder": folder})
            assert r.status_code == 200
            assert r.json()["total"] >= 1
            for item in r.json()["items"]:
                assert item["rel_path"].startswith(folder)

    r_nope = client.get("/api/images", params={"folder": "nonexistent/path/xyz"})
    assert r_nope.status_code == 200
    assert r_nope.json()["total"] == 0


def test_browse_images_sort_validation(client):
    """Invalid sort column returns 400."""
    r = client.get("/api/images", params={"sort": "evil_column"})
    assert r.status_code == 400
    assert "invalid sort" in r.json()["detail"].lower()


def test_browse_images_limit_capped(client):
    """Limit is capped by max_browse_limit."""
    r = client.get("/api/images", params={"limit": 99999})
    assert r.status_code == 200
    body = r.json()
    assert body["limit"] == client.app.state.settings.max_browse_limit
