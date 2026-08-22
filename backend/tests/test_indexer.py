"""Indexer tests with a deterministic fake embedder (no CLIP download)."""
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


def test_count_mismatch_forces_rebuild(env, monkeypatch):
    """ntotal != db rows (crash between upsert and add_with_ids) -> rebuild."""
    from backend.app import db

    ix, store, s = env
    _png(store / "a.png", (4, 4, 4))
    ix.incremental(trigger="t")
    assert ix.count == 1

    # Simulate divergence: a DB row whose vector never made it into the index.
    row_id = db.upsert_image(
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

    ix.load_or_create()  # detects mismatch, resets DB + index
    assert ix.count == 0 and db.count(s.db_path) == 0

    rep = ix.incremental(trigger="heal")
    assert rep.added == 1 and ix.count == 1


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

    def slow_walk():
        started.set()
        release.wait(timeout=10)  # simulate long scan work without the lock
        return original_walk()

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
