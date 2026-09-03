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
    assert out["results"][0]["thumb_url"].endswith("?v=png1")
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


def test_text_only_search(service):
    out = service.search(image_bytes=None, k=5, min_score=0.0, q="a.png")
    assert len(out["results"]) == 1
    assert out["results"][0]["rel_path"] == "a.png"
    assert out["results"][0]["score"] == 1.0
    assert out["results"][0]["source"] == "text"


def test_text_search_with_xmp_and_path(tmp_path, monkeypatch):
    monkeypatch.setattr(indexer_mod.embeddings, "embed_images", _fake_embed)
    monkeypatch.setattr(indexer_mod.metadata, "extract_xmp", lambda paths: {
        str(p): {"dc:title": "Project Alpha Rendering"} if "alpha.png" in str(p) else {}
        for p in paths
    })
    store = tmp_path / "xmp_store"
    store.mkdir()
    s = Settings(store_path=store, data_path=tmp_path / "xmp_data", run_initial_scan_on_start=False)
    p1 = store / "project_a" / "alpha.png"
    p1.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 8), (100, 100, 100)).save(p1)

    p2 = store / "project_b" / "beta.png"
    p2.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 8), (200, 200, 200)).save(p2)

    ix = indexer_mod.Indexer(s)
    ix.incremental(trigger="test_xmp")
    svc = search_mod.SearchService(ix, s)

    # Search by XMP tag text
    out_xmp = svc.search(image_bytes=None, k=5, min_score=0.0, q="Project Alpha")
    assert len(out_xmp["results"]) == 1
    assert out_xmp["results"][0]["rel_path"] == "project_a/alpha.png"

    # Search by folder/path text
    out_path = svc.search(image_bytes=None, k=5, min_score=0.0, q="project_b")
    assert len(out_path["results"]) == 1
    assert out_path["results"][0]["rel_path"] == "project_b/beta.png"

    # Image search combined with text query filter
    out_combined = svc.search(image_bytes=p1.read_bytes(), k=5, min_score=-1.0, q="beta")
    assert len(out_combined["results"]) == 1
    assert out_combined["results"][0]["rel_path"] == "project_b/beta.png"
    assert out_combined["results"][0]["source"] == "both"


def test_text_search_matches_xmp_tag_names(tmp_path, monkeypatch):
    monkeypatch.setattr(indexer_mod.embeddings, "embed_images", _fake_embed)
    monkeypatch.setattr(indexer_mod.metadata, "extract_xmp", lambda paths: {
        str(p): {"TransmissionReference": "Mars Shot", "Creator": "Jane Doe"} if "alpha.png" in str(p) else {}
        for p in paths
    })
    store = tmp_path / "tag_store"
    store.mkdir()
    s = Settings(store_path=store, data_path=tmp_path / "tag_data", run_initial_scan_on_start=False)
    p1 = store / "alpha.png"
    Image.new("RGB", (8, 8), (100, 100, 100)).save(p1)

    ix = indexer_mod.Indexer(s)
    ix.incremental(trigger="test_tag_names")
    svc = search_mod.SearchService(ix, s)

    out = svc.search(image_bytes=None, k=5, min_score=0.0, q="TransmissionReference")
    assert [row["rel_path"] for row in out["results"]] == ["alpha.png"]


def test_or_mode_unions_text_and_image_results(tmp_path, monkeypatch):
    monkeypatch.setattr(indexer_mod.embeddings, "embed_images", _fake_embed)
    monkeypatch.setattr(indexer_mod.metadata, "extract_xmp", lambda paths: {})
    store = tmp_path / "union_store"
    store.mkdir()
    s = Settings(store_path=store, data_path=tmp_path / "union_data", run_initial_scan_on_start=False)

    alpha = store / "alpha.png"
    Image.new("RGB", (8, 8), (10, 20, 30)).save(alpha)
    beta = store / "beta-project.png"
    Image.new("RGB", (8, 8), (220, 210, 200)).save(beta)

    ix = indexer_mod.Indexer(s)
    ix.incremental(trigger="test_union")
    svc = search_mod.SearchService(ix, s)

    out = svc.search(image_bytes=alpha.read_bytes(), k=5, min_score=0.0, q="beta", combine="or")

    assert {r["rel_path"] for r in out["results"]} == {"alpha.png", "beta-project.png"}
    by_rel = {r["rel_path"]: r for r in out["results"]}
    assert by_rel["beta-project.png"]["source"] == "both"
    assert by_rel["alpha.png"]["source"] == "image"
