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

    assert db.find_id_by_sha(p, "cc") == i1
    assert db.find_id_by_sha(p, "zz") is None

    db.remove_ids(p, [i1])
    assert db.count(p) == 1


def test_kv(tmp_path):
    p = tmp_path / "kv.db"
    db.init_db(p)
    assert db.kv_get(p, "model") is None
    db.kv_set(p, "model", "m")
    db.kv_set(p, "model", "m2")
    assert db.kv_get(p, "model") == "m2"


def test_reset(tmp_path):
    p = tmp_path / "r.db"
    db.init_db(p)
    db.upsert_image(p, rel_path="a", original_path="a", size=1, mtime=0.0,
                    sha256=None, width=None, height=None, xmp={})
    db.reset(p)
    assert db.count(p) == 0
    assert db.list_entries(p) == {}
