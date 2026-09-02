from types import SimpleNamespace

from PIL import Image

from backend.app import db
from backend.app.config import Settings
from backend.app import thumbs


def _environment(tmp_path, *, max_files=100):
    store = tmp_path / "store"
    store.mkdir()
    settings = Settings(
        store_path=store,
        data_path=tmp_path / "data",
        idle_thumbnails_enabled=True,
        idle_thumbnail_grace_sec=0,
        idle_thumbnail_delay_ms=0,
        idle_thumbnail_query_batch=2,
        thumbs_max_files=max_files,
    )
    settings.ensure_dirs()
    db.init_db(settings.db_path)
    indexer = SimpleNamespace(status={"state": "idle"})
    return settings, store, indexer


def _add_image(settings, store, rel_path, indexed_at):
    source = store / rel_path
    source.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (16, 16), (10, 20, 30)).save(source)
    image_id = db.upsert_image(
        settings.db_path,
        rel_path=rel_path,
        original_path=rel_path,
        size=source.stat().st_size,
        mtime=source.stat().st_mtime,
        sha256=None,
        width=16,
        height=16,
        xmp={},
    )
    with db.connect(settings.db_path) as conn, conn:
        conn.execute("UPDATE images SET indexed_at = ? WHERE id = ?", (indexed_at, image_id))
    return image_id


def test_worker_generates_newest_images_first(tmp_path):
    settings, store, indexer = _environment(tmp_path)
    old_id = _add_image(settings, store, "old.png", "2026-01-01T00:00:00Z")
    new_id = _add_image(settings, store, "new.png", "2026-02-01T00:00:00Z")
    worker = thumbs.IdleThumbnailWorker(settings, indexer)

    assert worker.run_once() is True
    assert (settings.thumbs_dir / f"{new_id}_{settings.thumb_size}.png").exists()
    assert not (settings.thumbs_dir / f"{old_id}_{settings.thumb_size}.png").exists()

    assert worker.run_once() is True
    assert (settings.thumbs_dir / f"{old_id}_{settings.thumb_size}.png").exists()


def test_worker_yields_to_scan_and_foreground_activity(tmp_path):
    settings, store, indexer = _environment(tmp_path)
    image_id = _add_image(settings, store, "image.png", "2026-01-01T00:00:00Z")
    cache = settings.thumbs_dir / f"{image_id}_{settings.thumb_size}.png"
    worker = thumbs.IdleThumbnailWorker(settings, indexer)

    worker.begin_foreground()
    assert worker.run_once() is False
    assert not cache.exists()
    worker.end_foreground()

    indexer.status["state"] = "scanning"
    assert worker.run_once() is False
    assert not cache.exists()

    indexer.status["state"] = "idle"
    assert worker.run_once() is True
    assert cache.exists()


def test_worker_stops_at_cache_capacity_without_pruning(tmp_path, monkeypatch):
    settings, store, indexer = _environment(tmp_path, max_files=1)
    _add_image(settings, store, "image.png", "2026-01-01T00:00:00Z")
    existing = settings.thumbs_dir / "999_256.png"
    existing.write_bytes(b"cached")
    monkeypatch.setattr(
        thumbs,
        "generate_thumbnail",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not generate")),
    )

    worker = thumbs.IdleThumbnailWorker(settings, indexer)

    assert worker.run_once() is False
    assert worker.snapshot()["state"] == "capacity"
    assert existing.exists()
