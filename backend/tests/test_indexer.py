"""Indexer tests with a deterministic fake embedder (no CLIP download)."""
import json
import threading
import time

import numpy as np
import pytest
from PIL import Image

from backend.app import indexer as indexer_mod
from backend.app.config import Settings

pytest.importorskip("faiss")

DIM = 16


def _fake_embed(images, settings):
    vecs = np.zeros((len(images), DIM), dtype=np.float32)
    for n, img in enumerate(images):
        seed = int.from_bytes(img.tobytes(), "little") % (2**32)
        rng = np.random.default_rng(seed)
        v = rng.standard_normal(DIM).astype(np.float32)
        vecs[n] = v / np.linalg.norm(v)
    return vecs


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(indexer_mod.embeddings, "embed_images", _fake_embed)
    monkeypatch.setattr(indexer_mod.metadata, "extract_xmp",
                        lambda paths: {str(p): {"Fake": "tag"} for p in paths})
    store = tmp_path / "store"
    data = tmp_path / "data"
    store.mkdir()
    settings = Settings(
        store_path=store,
        data_path=data,
        network_root=r"\\nas\renderings",
        batch_size=8,
        run_initial_scan_on_start=False,
    )
    return indexer_mod.Indexer(settings), store, settings


def _png(path, color):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 8), color).save(path)


def _touch_mtime(path, delta=100.0):
    import os
    st = os.stat(path)
    os.utime(path, (st.st_atime + delta, st.st_mtime + delta))


def test_add_update_remove_cycle(env):
    ix, store, s = env
    _png(store / "a.png", (255, 0, 0))
    _png(store / "sub" / "b.jpg", (0, 255, 0))

    rep = ix.incremental(trigger="test")
    assert (rep.added, rep.updated, report_removed(rep), rep.failed) == (2, 0, 0, 0)
    assert ix.count == 2
    assert s.index_file.exists()

    # second run: nothing changes
    rep = ix.incremental(trigger="test2")
    assert (rep.added, rep.updated, report_removed(rep)) == (0, 0, 0)

    # mtime change -> update; delete other file -> removed
    _touch_mtime(store / "a.png")
    (store / "sub" / "b.jpg").unlink()
    rep = ix.incremental(trigger="test3")
    assert (rep.added, rep.updated, report_removed(rep)) == (0, 1, 1)

    from backend.app import db
    row = db.get_by_id(s.db_path, db.list_entries(s.db_path)["a.png"].id)
    assert row["original_path"] == "\\\\nas\\renderings\\a.png"
    import json

    assert json.loads(row["xmp"]) == {"Fake": "tag"}
    assert row["width"] == 8 and row["height"] == 8


def report_removed(rep):
    return rep.removed


def test_failed_decode_is_counted(env, monkeypatch):
    ix, store, s = env
    _png(store / "good.png", (1, 2, 3))
    bad = store / "bad.png"
    bad.write_bytes(b"not an image")

    rep = ix.incremental(trigger="test")
    assert rep.added == 1 and rep.failed == 1
    assert any("bad.png" in e for e in rep.errors)


def test_trailing_all_fail_chunk_keeps_working_index(env):
    """A chunk whose decodes ALL fail must not drop the accumulated index."""
    ix, store, s = env
    for n in range(8):  # fills chunk 1 completely (batch_size=8)
        _png(store / f"g{n}.png", (n, 0, 0))
    (store / "zz.png").write_bytes(b"not an image")  # chunk 2: every decode fails

    rep = ix.incremental(trigger="t")
    assert rep.added == 8 and rep.failed == 1
    assert ix.count == 8           # index survived the empty chunk
    assert s.index_file.exists()   # and was persisted


def test_force_rebuild(env):
    ix, store, s = env
    _png(store / "a.png", (9, 9, 9))
    ix.incremental(trigger="t1")
    assert ix.count == 1
    rep = ix.incremental(trigger="t2", force_rebuild=True)
    assert rep.added == 1
    assert ix.count == 1


def test_persistence_across_instances(env, tmp_path):
    ix, store, s = env
    _png(store / "a.png", (5, 5, 5))
    ix.incremental(trigger="t")

    ix2 = indexer_mod.Indexer(s)
    ix2.load_or_create()
    assert ix2.count == 1


def test_initial_scan_can_use_store_snapshot_inventory(env, monkeypatch):
    ix, store, s = env
    _png(store / "a.png", (5, 5, 5))
    s.ensure_dirs()
    snapshot = {
        "version": 1,
        "created_utc": "2026-08-25T00:00:00Z",
        "root_path": str(store),
        "file_count": 1,
        "files": {"a.png": {"mtime": 123.0, "size": (store / "a.png").stat().st_size}},
    }
    s.store_snapshot_file.write_text(json.dumps(snapshot), encoding="utf-8")

    monkeypatch.setattr(ix, "_walk_store", lambda pause=None: pytest.fail("walk should be skipped"))

    rep = ix.incremental(trigger="initial")
    assert rep.added == 1
    assert ix.count == 1


def test_non_initial_full_scan_can_use_store_snapshot_inventory(env, monkeypatch):
    ix, store, s = env
    _png(store / "a.png", (5, 5, 5))
    ix.incremental(trigger="seed")
    _png(store / "b.png", (6, 6, 6))
    s.ensure_dirs()
    snapshot = {
        "version": 1,
        "created_utc": "2026-08-25T00:00:00Z",
        "root_path": str(store),
        "file_count": 2,
        "files": {
            "a.png": {"mtime": 123.0, "size": (store / "a.png").stat().st_size},
            "b.png": {"mtime": 124.0, "size": (store / "b.png").stat().st_size},
        },
    }
    s.store_snapshot_file.write_text(json.dumps(snapshot), encoding="utf-8")

    monkeypatch.setattr(ix, "_walk_store", lambda pause=None: pytest.fail("walk should be skipped"))

    rep = ix.incremental(trigger="rescan")
    assert rep.added == 1
    assert ix.status["inventory_source"] == "snapshot"


def test_delta_scan_uses_store_snapshot_metadata(env, monkeypatch):
    ix, store, s = env
    _png(store / "a.png", (5, 5, 5))
    ix.incremental(trigger="seed")
    _png(store / "b.png", (6, 6, 6))
    s.ensure_dirs()
    snapshot = {
        "version": 1,
        "created_utc": "2026-08-25T00:00:00Z",
        "root_path": str(store),
        "file_count": 2,
        "files": {
            "a.png": {"mtime": 123.0, "size": (store / "a.png").stat().st_size},
            "b.png": {"mtime": 124.0, "size": (store / "b.png").stat().st_size},
        },
    }
    s.store_snapshot_file.write_text(json.dumps(snapshot), encoding="utf-8")

    monkeypatch.setattr(ix, "_walk_store", lambda pause=None: pytest.fail("walk should be skipped"))
    original_exists = indexer_mod.Path.exists

    def exists_without_delta_file_stat(self):
        if self == store / "b.png":
            pytest.fail("delta scan should use snapshot metadata instead of probing the changed file")
        return original_exists(self)

    monkeypatch.setattr(indexer_mod.Path, "exists", exists_without_delta_file_stat)

    rep = ix.incremental(
        trigger="delta",
        delta_info={"changes": {"created": ["b.png"], "modified": [], "deleted": []}},
    )
    assert rep.added == 1
    assert ix.status["inventory_source"] == "snapshot"


def test_deleted_index_self_heals_via_full_rebuild(env, caplog):
    """Index file gone + intact DB -> loud warning, DB reset, next scan re-adds all."""
    import logging

    from backend.app import db

    ix, store, s = env
    _png(store / "a.png", (1, 1, 1))
    _png(store / "b.png", (2, 2, 2))
    ix.incremental(trigger="t")
    assert ix.count == 2 and s.index_file.exists()

    s.index_file.unlink()  # simulate lost/corrupt-removed index

    ix2 = indexer_mod.Indexer(s)
    with caplog.at_level(logging.WARNING):
        ix2.load_or_create()
    assert ix2.count == 0  # nothing silently searchable behind an intact-looking DB
    assert db.count(s.db_path) == 0  # DB reset so every file is re-embedded
    assert any("missing" in r.getMessage().lower() for r in caplog.records)

    rep = ix2.incremental(trigger="heal")
    assert rep.added == 2 and ix2.count == 2
    assert db.count(s.db_path) == 2


def test_corrupt_index_quarantined_and_rebuilt(env, tmp_path, caplog):
    """Garbage index file -> quarantined as .corrupt, no boot loop, rebuild works."""
    import logging

    from backend.app import db

    ix, store, s = env
    _png(store / "a.png", (3, 3, 3))
    ix.incremental(trigger="t")
    assert ix.count == 1

    s.index_file.write_bytes(b"CORRUPTED FAISS PAYLOAD")  # unreadable

    ix2 = indexer_mod.Indexer(s)
    with caplog.at_level(logging.WARNING):
        ix2.load_or_create()
    corrupt = s.index_file.with_suffix(".faiss.corrupt")
    assert corrupt.exists() and not s.index_file.exists()
    assert ix2.index is None
    assert db.count(s.db_path) == 0
    assert any("quarantin" in r.getMessage().lower() for r in caplog.records)

    rep = ix2.incremental(trigger="heal")
    assert rep.added == 1 and ix2.count == 1
    assert s.index_file.exists()


def test_db_ahead_divergence_is_repaired_surgically(env):
    """DB rows whose vector never made it into the index (crash between
    upsert and add_with_ids) get pruned; everything else survives. A restart
    must NOT wipe the whole index (that caused the rescan-from-zero loop)."""
    from backend.app import db

    ix, store, s = env
    _png(store / "a.png", (4, 4, 4))
    ix.incremental(trigger="t")
    original_id = db.list_entries(s.db_path)["a.png"].id
    assert ix.count == 1

    # Simulate divergence: a DB row whose vector never made it into the index.
    db.upsert_image(
        s.db_path,
        rel_path="ghost.png",
        original_path="ghost.png",
        size=1,
        mtime=1.0,
        sha256=None,
        width=8,
        height=8,
        xmp={},
    )
    assert db.count(s.db_path) == 2 and ix.count == 1

    ix2 = indexer_mod.Indexer(s)
    ix2.load_or_create()

    # Surgical repair: ghost row gone, real data untouched (same id => no wipe).
    assert db.count(s.db_path) == 1
    assert db.list_entries(s.db_path)["a.png"].id == original_id
    assert ix2.count == 1
    assert s.index_file.exists()

    rep = ix2.incremental(trigger="heal")
    assert rep.added == 0 and rep.failed == 0


def test_orphan_vectors_are_removed_on_boot(env):
    """Vector ids without a DB row are dropped at startup (legacy/corrupt
    states); search can never return ids the DB cannot resolve."""
    import faiss

    from backend.app import db

    ix, store, s = env
    _png(store / "a.png", (7, 7, 7))
    ix.incremental(trigger="t")
    assert ix.count == 1

    # Remove the DB row behind the published index's back.
    db.remove_ids(s.db_path, [db.list_entries(s.db_path)["a.png"].id])

    ix2 = indexer_mod.Indexer(s)
    ix2.load_or_create()
    assert ix2.count == 0
    assert db.count(s.db_path) == 0
    assert int(faiss.vector_to_array(ix2.index.id_map).size) == 0


def test_id_sets_diverged_with_equal_counts_still_repaired(env):
    """Equal counts but different ids (e.g. re-created DB rows) must not pass
    a pure count compare — the sets themselves have to match."""
    from backend.app import db

    ix, store, s = env
    _png(store / "a.png", (8, 8, 8))
    ix.incremental(trigger="t")
    old_id = db.list_entries(s.db_path)["a.png"].id

    # Recreate the row: AUTOINCREMENT assigns a different id, count stays 1.
    conn = __import__("sqlite3").connect(s.db_path)
    with conn:
        conn.execute("DELETE FROM images")
    conn.close()
    db.upsert_image(
        s.db_path,
        rel_path="a.png",
        original_path="a.png",
        size=(store / "a.png").stat().st_size,
        mtime=(store / "a.png").stat().st_mtime,
        sha256=None,
        width=8,
        height=8,
        xmp={},
    )
    new_id = db.list_entries(s.db_path)["a.png"].id
    assert new_id != old_id

    ix2 = indexer_mod.Indexer(s)
    ix2.load_or_create()
    # Old vector id removed; row pruned too (its id has no vector) -> empty,
    # and the next scan re-embeds the file cleanly.
    assert ix2.count == 0 and db.count(s.db_path) == 0
    rep = ix2.incremental(trigger="heal")
    assert rep.added == 1 and ix2.count == 1


def test_interrupted_scan_loses_at_most_one_chunk(env, monkeypatch):
    """A scan killed mid-run must not doom the next startup to a full rebuild:
    periodic publishes keep the persisted index near the per-chunk DB commits,
    startup repairs the remainder surgically."""
    ix, store, s = env
    monkeypatch.setattr(indexer_mod, "PUBLISH_INTERVAL_SEC", 0.0)
    ix.settings.batch_size = 1  # one file per chunk so the crash lands between files
    _png(store / "a.png", (1, 0, 0))
    ix.incremental(trigger="seed")
    from backend.app import db

    seed_id = db.list_entries(s.db_path)["a.png"].id

    _png(store / "b.png", (2, 0, 0))
    _png(store / "c.png", (3, 0, 0))

    original_process = ix._process_chunk
    calls = {"n": 0}

    def crashing_process(*args, **kwargs):
        result = original_process(*args, **kwargs)
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated crash after first chunk")
        return result

    monkeypatch.setattr(ix, "_process_chunk", crashing_process)
    with pytest.raises(RuntimeError):
        ix.incremental(trigger="doomed")

    # Restart with a fresh instance, exactly like a container restart.
    ix2 = indexer_mod.Indexer(s)
    ix2.load_or_create()
    # b.png was fully committed+published before the crash -> survives.
    assert ix2.count == 2
    assert {"a.png", "b.png"} == set(db.list_entries(s.db_path))
    assert db.list_entries(s.db_path)["a.png"].id == seed_id

    rep = ix2.incremental(trigger="continue")
    assert rep.added == 1            # only c.png left to embed
    assert ix2.count == 3


def test_long_scan_publishes_progress_periodically(env, monkeypatch):
    ix, store, s = env
    monkeypatch.setattr(indexer_mod, "PUBLISH_INTERVAL_SEC", 0.0)
    for n in range(3):
        _png(store / f"p{n}.png", (n, 0, 0))

    published = []
    original_publish = ix._publish_index

    def spying_publish(index):
        published.append(int(index.ntotal) if index is not None else 0)
        return original_publish(index)

    monkeypatch.setattr(ix, "_publish_index", spying_publish)
    ix.incremental(trigger="progressive")

    # First chunk publishes immediately (throttle starts at -inf), final
    # publish closes the scan; nothing in between may skip publishing here
    # because every chunk is due again at interval=0.
    assert len(published) >= 2
    assert published[-1] == 3


def test_decode_downscales_but_keeps_original_dimensions(env, monkeypatch):
    """Scan-time decode shrinks frames before they enter the prefetch window
    (OOM guard for 100+ Mpixel renders) while DB rows keep the ORIGINAL
    dimensions."""
    ix, store, s = env
    from backend.app import db

    real_embed = indexer_mod.embeddings.embed_images
    sizes_seen: list[tuple[int, int]] = []

    def spy_embed(images, settings):
        sizes_seen.extend(img.size for img in images)
        return real_embed(images, settings)

    monkeypatch.setattr(indexer_mod.embeddings, "embed_images", spy_embed)

    Image.new("RGB", (2000, 1000), (9, 9, 9)).save(store / "big.png")
    rep = ix.incremental(trigger="t")
    assert rep.added == 1

    assert sizes_seen and all(
        w <= indexer_mod.SCAN_DECODE_MAX_SIDE and h <= indexer_mod.SCAN_DECODE_MAX_SIDE
        for w, h in sizes_seen
    )
    row = db.get_by_id(s.db_path, db.list_entries(s.db_path)["big.png"].id)
    assert (row["width"], row["height"]) == (2000, 1000)


def test_scan_never_mutates_published_index(env):
    """H1 regression: an incremental scan must clone the published FAISS index
    before remove_ids/add_with_ids. Mutating the object that lock-free search
    readers hold is undefined behavior (FAISS releases the GIL)."""
    ix, store, s = env
    _png(store / "a.png", (1, 0, 0))
    _png(store / "b.png", (2, 0, 0))
    ix.incremental(trigger="seed")
    assert ix.count == 2

    # A reader grabs the published object (as SearchService does) and keeps it.
    published = ix.index
    n_before = int(published.ntotal)
    assert n_before == 2

    # Force every mutation path: one deletion + one update (remove + re-add).
    (store / "b.png").unlink()
    _touch_mtime(store / "a.png")
    rep = ix.incremental(trigger="mutate")
    assert (rep.removed, rep.updated) == (1, 1)

    # The old generation was never touched in place...
    assert int(published.ntotal) == n_before == 2
    # ...and the newly published generation is a different object that
    # reflects the changes.
    assert ix.index is not published
    assert ix.count == 1


def test_scan_does_not_hold_lock_during_faiss_query(env):
    """Searches must proceed while a scan is running (lock decoupling)."""
    import threading

    ix, store, s = env
    _png(store / "a.png", (6, 6, 6))
    ix.incremental(trigger="t")

    started = threading.Event()
    release = threading.Event()
    original_walk = ix._walk_store
    result: dict = {}

    def slow_walk(pause=None):
        started.set()
        release.wait(timeout=10)  # simulate long scan work without the lock
        return original_walk(pause)

    def run_scan():
        try:
            result["report"] = ix.incremental(trigger="slow")
        except Exception as exc:  # noqa: BLE001
            result["error"] = exc

    ix._walk_store = slow_walk
    t = threading.Thread(target=run_scan, daemon=True)
    try:
        t.start()
        assert started.wait(timeout=5)
        # Scan mid-flight: the search-side lock must be free.
        acquired = ix.lock.acquire(timeout=2)
        if acquired:
            ix.lock.release()
        assert acquired, "scan must not hold Indexer._lock during work"
    finally:
        release.set()
        t.join(timeout=10)
        ix._walk_store = original_walk
    assert "report" in result


def test_scan_publishes_live_report_for_inventory_and_batches(env):
    ix, store, _settings = env
    for n in range(9):
        _png(store / f"image-{n}.png", (n, 0, 0))

    updates = []
    ix.incremental(trigger="progress", progress=updates.append)

    assert updates[0]["seen"] == 9
    assert {update["processed"] for update in updates} >= set(range(10))
    assert updates[-1]["processed"] == 9
    assert updates[-1]["added"] == 9
    assert ix.status["state"] == "idle"
    assert ix.status["last_report"]["added"] == 9


def test_scan_can_pause_and_resume_between_files(env, monkeypatch):
    ix, store, _settings = env
    ix.settings.batch_size = 1
    _png(store / "a.png", (1, 0, 0))
    _png(store / "b.png", (2, 0, 0))

    first_batch_done = threading.Event()
    release_first = threading.Event()
    original_process = ix._process_chunk
    calls = {"n": 0}

    def slow_process(*args, **kwargs):
        result = original_process(*args, **kwargs)
        calls["n"] += 1
        if calls["n"] == 1:
            first_batch_done.set()
            assert release_first.wait(timeout=5)
        return result

    monkeypatch.setattr(ix, "_process_chunk", slow_process)

    result = {}

    def run_scan():
        result["report"] = ix.incremental(trigger="pause-test")

    worker = threading.Thread(target=run_scan, daemon=True)
    worker.start()
    assert first_batch_done.wait(timeout=5)
    assert ix.request_pause()
    release_first.set()

    deadline = time.time() + 5
    while time.time() < deadline and ix.status["state"] != "paused":
        time.sleep(0.01)

    assert ix.status["state"] == "paused"
    assert worker.is_alive()
    assert ix.resume()

    worker.join(timeout=10)
    assert "report" in result
    assert result["report"].added == 2
    assert ix.status["state"] == "idle"


def test_scan_can_pause_inside_large_batch(env, monkeypatch):
    ix, store, _settings = env
    ix.settings.batch_size = 8
    _png(store / "a.png", (1, 0, 0))
    _png(store / "b.png", (2, 0, 0))
    _png(store / "c.png", (3, 0, 0))

    entered_second = threading.Event()
    release_second = threading.Event()
    original_decode = indexer_mod.embeddings.decode_image
    calls = {"n": 0}

    def slow_decode(path):
        calls["n"] += 1
        if calls["n"] == 2:
            entered_second.set()
            assert release_second.wait(timeout=5)
        return original_decode(path)

    monkeypatch.setattr(indexer_mod.embeddings, "decode_image", slow_decode)

    result = {}

    def run_scan():
        result["report"] = ix.incremental(trigger="pause-mid-batch")

    worker = threading.Thread(target=run_scan, daemon=True)
    worker.start()
    assert entered_second.wait(timeout=5)
    assert ix.request_pause()
    release_second.set()

    deadline = time.time() + 5
    while time.time() < deadline and ix.status["state"] != "paused":
        time.sleep(0.01)

    assert ix.status["state"] == "paused"
    assert ix.resume()
    worker.join(timeout=10)
    assert "report" in result
    assert result["report"].added == 3
    assert ix.status["state"] == "idle"


def test_resume_checkpoint_restores_paused_scan_after_restart(env, monkeypatch):
    ix, store, settings = env
    _png(store / "a.png", (1, 1, 1))
    ix.incremental(trigger="seed-a")
    _png(store / "b.png", (2, 2, 2))
    settings.ensure_dirs()
    snapshot = {
        "version": 1,
        "created_utc": "2026-08-25T00:00:00Z",
        "root_path": str(store),
        "file_count": 2,
        "files": {
            "a.png": {"mtime": 123.0, "size": (store / "a.png").stat().st_size},
            "b.png": {"mtime": 124.0, "size": (store / "b.png").stat().st_size},
        },
    }
    settings.store_snapshot_file.write_text(json.dumps(snapshot), encoding="utf-8")

    checkpoint_path = settings.data_path / "scan_checkpoint.json"
    checkpoint_path.write_text(
        '{"version":1,"phase":"pending","mode":"full","force_rebuild":false,'
        '"trigger":"resume-test","model":"ViT-B-32-quickgelu:openai",'
        '"report":{"trigger":"resume-test","started_at":1.0,"duration_sec":0.0,'
        '"seen":2,"processed":1,"added":1,"updated":0,"removed":0,'
        '"unchanged":0,"failed":0,"error_count":0},'
        '"remaining_rel_paths":["b.png"],"remaining_added_rel_paths":["b.png"],'
        '"updated_at":"2026-01-01T00:00:00Z"}',
        encoding="utf-8",
    )

    decoded = []
    original_decode = indexer_mod.embeddings.decode_image

    def tracking_decode(path):
        decoded.append(path.name)
        return original_decode(path)

    monkeypatch.setattr(indexer_mod.embeddings, "decode_image", tracking_decode)

    resumed = indexer_mod.Indexer(settings)
    resumed.load_or_create()
    assert resumed.status["state"] == "paused"
    assert resumed.count == 1

    report = resumed.resume_from_checkpoint()
    assert report.added == 2
    assert resumed.count == 2
    assert decoded == ["b.png"]
    assert resumed.status["inventory_source"] == "snapshot"
    assert not checkpoint_path.exists()


def test_stale_planning_checkpoint_is_discarded_on_boot(env, caplog):
    """A crash/restart mid-scan leaves a 'planning'-phase checkpoint behind.
    Booting with it must NOT flip the app into a phantom 'paused' state:
    that blocked RUN_INITIAL_SCAN_ON_START (trigger_now refuses while paused)
    and made every container restart look like a rescan stuck at zero."""
    import logging

    ix, store, settings = env
    _png(store / "a.png", (1, 1, 1))
    ix.incremental(trigger="seed")
    settings.ensure_dirs()
    checkpoint_path = settings.data_path / "scan_checkpoint.json"
    checkpoint_path.write_text(
        '{"version":1,"phase":"planning","mode":"full","force_rebuild":false,'
        '"trigger":"manual-api","model":"ViT-B-32-quickgelu:openai",'
        '"report":{"trigger":"manual-api","started_at":1.0,"duration_sec":0.0,'
        '"seen":0,"processed":0,"added":0,"updated":0,"removed":0,'
        '"unchanged":0,"failed":0,"error_count":0},'
        '"delta_info":null,'
        '"updated_at":"2026-01-01T00:00:00Z"}',
        encoding="utf-8",
    )

    ix2 = indexer_mod.Indexer(settings)
    with caplog.at_level(logging.INFO):
        ix2.load_or_create()
    assert ix2.status["state"] == "idle"
    assert not checkpoint_path.exists()
    assert any("discarding stale" in r.getMessage() for r in caplog.records)

    # Auto initial scan (or manual rescan) can start right away.
    rep = ix2.incremental(trigger="initial-scan")
    assert rep.unchanged == 1 and rep.failed == 0
    assert ix2.status["state"] == "idle"


def test_resume_replay_of_committed_files_does_not_duplicate(env):
    """Resume from a STALE checkpoint replays files the crashed run had
    already committed+published (after the last pause snapshot). Re-adding
    them must not leave two vectors under one id."""
    import faiss

    ix, store, settings = env
    _png(store / "a.png", (1, 1, 1))
    ix.incremental(trigger="seed")
    _png(store / "b.png", (2, 2, 2))
    settings.ensure_dirs()
    snapshot = {
        "version": 1,
        "created_utc": "2026-08-26T00:00:00Z",
        "root_path": str(store),
        "file_count": 2,
        "files": {
            "a.png": {"mtime": 123.0, "size": (store / "a.png").stat().st_size},
            "b.png": {"mtime": 124.0, "size": (store / "b.png").stat().st_size},
        },
    }
    settings.store_snapshot_file.write_text(json.dumps(snapshot), encoding="utf-8")

    # Stale checkpoint: b.png listed as remaining "added" work, although a
    # later chunk of the crashed run already committed+published it.
    checkpoint_path = settings.data_path / "scan_checkpoint.json"
    checkpoint_path.write_text(
        '{"version":1,"phase":"pending","mode":"full","force_rebuild":false,'
        '"trigger":"resume-test","model":"ViT-B-32-quickgelu:openai",'
        '"report":{"trigger":"resume-test","started_at":1.0,"duration_sec":0.0,'
        '"seen":2,"processed":1,"added":1,"updated":0,"removed":0,'
        '"unchanged":0,"failed":0,"error_count":0},'
        '"remaining_rel_paths":["b.png"],"remaining_added_rel_paths":["b.png"],'
        '"updated_at":"2026-01-01T00:00:00Z"}',
        encoding="utf-8",
    )

    resumed = indexer_mod.Indexer(settings)
    resumed.load_or_create()
    assert resumed.status["state"] == "paused"
    resumed.resume_from_checkpoint()

    ids = [int(i) for i in faiss.vector_to_array(resumed.index.id_map)]
    assert len(ids) == len(set(ids)) == 2   # one vector per file, no dupes
    assert resumed.count == 2


def test_boot_repair_deduplicates_existing_duplicate_vectors(env):
    """Legacy indexes with two vectors under one id (pre-fix resume replay)
    are cleaned at startup: all copies dropped + row pruned -> clean re-embed."""
    import faiss
    import numpy as np

    from backend.app import db

    ix, store, s = env
    _png(store / "a.png", (3, 3, 3))
    ix.incremental(trigger="seed")

    # Corrupt the persisted index the way old resume replays did.
    idx = faiss.read_index(str(s.index_file))
    row_id = db.list_entries(s.db_path)["a.png"].id
    vec = np.zeros((1, 16), dtype="float32")
    idx.add_with_ids(vec, np.array([row_id], dtype="int64"))
    faiss.write_index(idx, str(s.index_file))
    assert int(idx.ntotal) == 2 and len(faiss.vector_to_array(idx.id_map)) == 2

    ix2 = indexer_mod.Indexer(s)
    ix2.load_or_create()

    assert ix2.count == 0                      # both copies gone...
    assert db.count(s.db_path) == 0            # ...and row pruned for re-embed
    rep = ix2.incremental(trigger="heal")
    assert rep.added == 1 and ix2.count == 1


def test_scan_report_speed_metrics():
    report = indexer_mod.ScanReport(
        trigger="test",
        started_at=time.time() - 60.0,
        duration_sec=60.0,
        seen=120,
        processed=120,
        added=30,
        updated=30,
    )
    d = report.as_dict()
    assert d["elapsed_sec"] == 60.0
    assert d["scans_per_min"] == 120.0
    assert d["embeddings_per_min"] == 60.0
    assert d["scans_per_sec"] == 2.0
    assert d["embeddings_per_sec"] == 1.0
