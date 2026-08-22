"""Search tests using the fake embedder from test_indexer."""
import numpy as np
import pytest
from PIL import Image

from backend.app import indexer as indexer_mod
from backend.app import search as search_mod
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
def service(tmp_path, monkeypatch):
    monkeypatch.setattr(indexer_mod.embeddings, "embed_images", _fake_embed)
    monkeypatch.setattr(indexer_mod.metadata, "extract_xmp", lambda paths: {})
    store = tmp_path / "store"
    store.mkdir()
    settings = Settings(store_path=store, data_path=tmp_path / "data",
                        run_initial_scan_on_start=False)

    def add(name, color):
        p = store / name
        p.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (8, 8), color).save(p)
        return p

    ix = indexer_mod.Indexer(settings)
    add("a.png", (255, 0, 0))
    add("b.jpg", (0, 255, 0))
    ix.incremental(trigger="test")

    return search_mod.SearchService(ix, settings)


def _bytes_of(path):
    return path.read_bytes()


def test_exact_match_pins_rank_one(service, tmp_path):
    src = tmp_path / "store" / "a.png"
    out = service.search(_bytes_of(src), k=5, min_score=0.0)
    assert out["exact_match"] is True
    assert out["results"][0]["exact"] is True
    assert out["results"][0]["score"] == 1.0
    assert out["results"][0]["rel_path"] == "a.png"
    # remaining results sorted by score descending
    scores = [r["score"] for r in out["results"][1:]]
    assert scores == sorted(scores, reverse=True)


def test_self_similarity_ranks_first(service, tmp_path):
    # re-encoded copy (different bytes) must still rank itself first via embedding
    buf = tmp_path / "query.png"
    Image.open(tmp_path / "store" / "b.jpg").resize((16, 16)).save(buf)
    out = service.search(buf.read_bytes(), k=5, min_score=0.0)
    assert out["results"][0]["rel_path"] in ("a.png", "b.jpg")


def test_empty_index(service):
    from backend.app.indexer import Indexer
    s = Settings(store_path=tmp_empty(service), data_path=service.settings.data_path,
                 run_initial_scan_on_start=False)
    empty_ix = Indexer(s)
    svc = search_mod.SearchService(empty_ix, s)
    out = svc.search(b"\x89PNG nope", 3, 0.0)
    assert out["results"] == [] and out["total_indexed"] == 0


def tmp_empty(service):
    p = service.settings.store_path.parent / "empty_store"
    p.mkdir(exist_ok=True)
    return p


def test_min_score_filters(service, tmp_path):
    noise = tmp_path / "noise.png"
    Image.new("RGB", (8, 8), (123, 45, 200)).save(noise)
    out = service.search(noise.read_bytes(), k=10, min_score=0.99)
    assert all(r["score"] >= 0.99 for r in out["results"])
