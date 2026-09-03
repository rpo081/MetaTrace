from types import SimpleNamespace

import concurrent.futures as cf
import os
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
    settings.thumbs_prune_buffer = 0
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


def test_worker_allows_buffer_before_capacity(tmp_path):
    settings, store, indexer = _environment(tmp_path, max_files=1)
    settings.thumbs_prune_buffer = 1
    image_id = _add_image(settings, store, "image.png", "2026-01-01T00:00:00Z")
    existing = settings.thumbs_dir / "999_256.png"
    existing.write_bytes(b"cached")
    worker = thumbs.IdleThumbnailWorker(settings, indexer)

    assert worker.run_once() is True
    assert (settings.thumbs_dir / f"{image_id}_{settings.thumb_size}.png").exists()


def test_idle_worker_yields_while_bulk_worker_runs(tmp_path):
    settings, store, indexer = _environment(tmp_path)
    _add_image(settings, store, "image.png", "2026-01-01T00:00:00Z")
    worker = thumbs.IdleThumbnailWorker(settings, indexer)
    worker.set_bulk_worker(SimpleNamespace(is_running=lambda: True))

    assert worker.run_once() is False
    assert worker.snapshot()["state"] == "waiting"


def test_bulk_worker_generates_missing_thumbnails(tmp_path, monkeypatch):
    settings, store, indexer = _environment(tmp_path)
    settings.admin_thumbnail_workers = 2
    first_id = _add_image(settings, store, "new.png", "2026-02-01T00:00:00Z")
    second_id = _add_image(settings, store, "old.png", "2026-01-01T00:00:00Z")

    class FakeExecutor:
        def __init__(self, *, max_workers, mp_context):
            self.max_workers = max_workers
            self.mp_context = mp_context

        def submit(self, fn, *args):
            future = cf.Future()
            try:
                future.set_result(fn(*args))
            except Exception as exc:  # noqa: BLE001
                future.set_exception(exc)
            return future

        def shutdown(self, wait=False, cancel_futures=True):
            return None

    monkeypatch.setattr(thumbs, "ProcessPoolExecutor", FakeExecutor)

    worker = thumbs.BulkThumbnailWorker(settings, indexer)

    assert worker.start() is True
    deadline = __import__("time").time() + 3
    while worker.is_running() and __import__("time").time() < deadline:
        pass

    assert worker.snapshot()["state"] == "complete"
    assert worker.snapshot()["generated"] == 2
    assert (settings.thumbs_dir / f"{first_id}_{settings.thumb_size}.png").exists()
    assert (settings.thumbs_dir / f"{second_id}_{settings.thumb_size}.png").exists()


def test_bulk_worker_rejects_start_while_scan_active(tmp_path):
    settings, store, indexer = _environment(tmp_path)
    indexer.status["state"] = "scanning"
    worker = thumbs.BulkThumbnailWorker(settings, indexer)

    try:
        worker.start()
    except RuntimeError as exc:
        assert str(exc) == "scan_active"
    else:
        raise AssertionError("expected scan_active runtime error")


def test_bulk_worker_reduces_target_workers_during_foreground_activity(tmp_path):
    settings, store, indexer = _environment(tmp_path)
    settings.admin_thumbnail_workers = 6
    settings.admin_thumbnail_foreground_workers = 2
    settings.idle_thumbnail_grace_sec = 10
    worker = thumbs.BulkThumbnailWorker(settings, indexer)

    assert worker.snapshot()["target_workers"] == 6

    worker.begin_foreground()
    assert worker.snapshot()["target_workers"] == 1

    worker.end_foreground()
    assert worker.snapshot()["target_workers"] == 2


def test_prune_thumb_cache_ignores_missing_files_during_stat_and_unlink(tmp_path, monkeypatch):
    settings, _, _ = _environment(tmp_path, max_files=1)
    settings.thumbs_prune_buffer = 0
    first = settings.thumbs_dir / "1_256.png"
    second = settings.thumbs_dir / "2_256.png"
    first.write_bytes(b"a")
    second.write_bytes(b"b")

    original_stat = os.stat
    original_unlink = os.unlink

    def flaky_stat(path, *args, **kwargs):
        if str(path).endswith("2_256.png"):
            raise FileNotFoundError()
        return original_stat(path, *args, **kwargs)

    def flaky_unlink(path, *args, **kwargs):
        if str(path).endswith("1_256.png"):
            raise FileNotFoundError()
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", flaky_stat)
    monkeypatch.setattr(os, "unlink", flaky_unlink)

    assert thumbs.prune_thumb_cache(settings, max_files=0) == 0
    assert thumbs.prune_thumb_cache(settings) == 0


def test_prune_thumb_cache_waits_for_buffer_then_prunes_back_to_cap(tmp_path):
    settings, _, _ = _environment(tmp_path, max_files=2)
    settings.thumbs_prune_buffer = 1
    first = settings.thumbs_dir / "1_256.png"
    second = settings.thumbs_dir / "2_256.png"
    third = settings.thumbs_dir / "3_256.png"
    fourth = settings.thumbs_dir / "4_256.png"

    first.write_bytes(b"a")
    second.write_bytes(b"b")
    third.write_bytes(b"c")

    assert thumbs.prune_thumb_cache(settings) == 0
    assert sum(1 for _ in settings.thumbs_dir.glob("*.png")) == 3

    fourth.write_bytes(b"d")
    assert thumbs.prune_thumb_cache(settings) == 2
    assert sum(1 for _ in settings.thumbs_dir.glob("*.png")) == 2
