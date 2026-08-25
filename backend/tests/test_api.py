"""API tests with a temp store; no CLIP model needed for these endpoints."""
import io

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from backend.app import embeddings, metadata
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

    monkeypatch.setenv("METATRACE_ADMIN_TOKEN", "abc")
    monkeypatch.setenv("METATRACE_CORS_ORIGINS", "http://a.example, http://b.example")
    s = S()
    assert s.admin_token == "abc"
    assert s.cors_origin_list == ["http://a.example", "http://b.example"]
    monkeypatch.delenv("METATRACE_ADMIN_TOKEN")
    monkeypatch.delenv("METATRACE_CORS_ORIGINS")
    s2 = S()
    assert s2.admin_token is None and s2.cors_origins is None


def test_stats_sanitized_and_parity(client):
    """No absolute paths leak; db_count/max_upload_mb exposed for parity."""
    r = client.get("/api/stats")
    body = r.json()
    assert r.status_code == 200
    for key in ("indexed", "db_count", "state", "last_report", "last_scan",
                "model", "exiftool", "max_upload_mb"):
        assert key in body
    # sanitized: no store/network topology disclosure
    assert "store_path" not in body
    assert "network_root" not in body
    assert str(client.app.state.settings.store_path) not in r.text
    # parity observability
    assert body["indexed"] == body["db_count"] == 1
    assert body["max_upload_mb"] == client.app.state.settings.max_upload_mb


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


def test_search_requires_file(client):
    r = client.post("/api/search")
    assert r.status_code in (400, 422)


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
    from backend.app.api import routes as routes_mod
    monkeypatch.setattr(routes_mod, "_warned_trusted_lan", False)  # order-independent
    with caplog.at_level("WARNING"):
        r = client.post("/api/rescan")
    assert r.status_code == 202
    warnings = [rec for rec in caplog.records
                if "METATRACE_ADMIN_TOKEN" in rec.getMessage()]
    assert len(warnings) == 1  # one-time warning
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
