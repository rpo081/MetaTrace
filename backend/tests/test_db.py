import sqlite3

import backend.app.db as db


def test_upsert_and_fetch(tmp_path):
    p = tmp_path / "t.db"
    db.init_db(p)
    i1 = db.upsert_image(p, rel_path="a/x.png", original_path=r"\\nas\x.png",
                         size=10, mtime=1.0, sha256="aa", width=3, height=4, xmp={"Title": "T"})
    i2 = db.upsert_image(p, rel_path="b/y.jpg", original_path=r"\\nas\y.jpg",
                         size=20, mtime=2.0, sha256="bb", width=None, height=None, xmp={})
    assert i1 != i2
    assert db.count(p) == 2

    # upsert keeps the same id and updates fields
    again = db.upsert_image(p, rel_path="a/x.png", original_path=r"\\nas\x.png",
                            size=11, mtime=9.0, sha256="cc", width=3, height=4, xmp={})
    assert again == i1
    entries = db.list_entries(p)
    assert entries["a/x.png"].size == 11

    rows = db.fetch_by_ids(p, [i2, i1])
    assert [r["id"] for r in rows] == [i2, i1]
    import json

    assert json.loads(rows[1]["xmp"]) == {}

    db.remove_ids(p, [i1])
    assert db.count(p) == 1


def test_kv(tmp_path):
    p = tmp_path / "kv.db"
    db.init_db(p)
    assert db.kv_get(p, "model") is None
    db.kv_set(p, "model", "m")
    db.kv_set(p, "model", "m2")
    assert db.kv_get(p, "model") == "m2"


def test_bulk_upsert_matches_single_upsert(tmp_path):
    p = tmp_path / "bulk.db"
    db.init_db(p)
    assert db.upsert_images_bulk(p, []) == {}

    rows = [
        {"rel_path": "a/x.png", "original_path": r"\\nas\x.png", "size": 10,
         "mtime": 1.0, "sha256": "aa", "width": 3, "height": 4,
         "xmp": {"Title": "T"}},
        {"rel_path": "b/y.jpg", "original_path": r"\\nas\y.jpg", "size": 20,
         "mtime": 2.0, "sha256": "bb", "width": None, "height": None, "xmp": {}},
    ]
    ids = db.upsert_images_bulk(p, rows)
    assert set(ids) == {"a/x.png", "b/y.jpg"}
    assert ids["a/x.png"] != ids["b/y.jpg"]
    assert db.count(p) == 2

    single_id = db.upsert_image(
        p, rel_path="c/z.png", original_path="z", size=5, mtime=3.0,
        sha256=None, width=8, height=8, xmp={},
    )
    again = db.upsert_images_bulk(
        p, [{**rows[0], "size": 11, "mtime": 9.0, "sha256": "cc"}]
    )
    assert again["a/x.png"] == ids["a/x.png"]

    entries = db.list_entries(p)
    assert entries["a/x.png"].size == 11
    assert entries["a/x.png"].id == ids["a/x.png"]
    assert entries["c/z.png"].id == single_id

    import json

    row_a = db.get_by_id(p, ids["a/x.png"])
    assert json.loads(row_a["xmp"]) == {"Title": "T"}
    row_b = db.get_by_id(p, ids["b/y.jpg"])
    assert json.loads(row_b["xmp"]) == {}


def test_bulk_upsert_spans_id_fetch_slices(tmp_path):
    p = tmp_path / "slices.db"
    db.init_db(p)
    n = db._ID_FETCH_SLICE + 120
    rows = [
        {"rel_path": f"f/{i}.png", "original_path": f"f/{i}", "size": i,
         "mtime": float(i), "sha256": None, "width": None, "height": None,
         "xmp": {}}
        for i in range(n)
    ]
    ids = db.upsert_images_bulk(p, rows)
    assert len(ids) == n and len(set(ids.values())) == n
    assert all(ids[r["rel_path"]] > 0 for r in rows)


def test_reset(tmp_path):
    p = tmp_path / "r.db"
    db.init_db(p)
    db.upsert_image(p, rel_path="a", original_path="a", size=1, mtime=0.0,
                    sha256=None, width=None, height=None, xmp={})
    db.reset(p)
    assert db.count(p) == 0
    assert db.list_entries(p) == {}


def test_init_db_disables_wal_churn_for_short_read_connections(tmp_path):
    p = tmp_path / "wal.db"
    with sqlite3.connect(p) as conn:
        mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        assert mode.lower() == "wal"

    db.init_db(p)
    with sqlite3.connect(p) as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "delete"

    db.count(p)
    db.kv_get(p, "last_scan")
    assert not p.with_suffix(".db-wal").exists()
    assert not p.with_suffix(".db-shm").exists()
