import json

from backend.app.config import Settings
from backend.app.file_rules import MAX_SEQUENCE_IMAGES
from backend.app.scanner import _load_snapshot_inventory, _walk_store


def _settings(tmp_path) -> Settings:
    settings = Settings(
        store_path=tmp_path / "store",
        data_path=tmp_path / "data",
        use_store_snapshot_for_initial_scan=True,
        snapshot_max_age_hours=0,
    )
    settings.ensure_dirs()
    settings.store_path.mkdir(parents=True, exist_ok=True)
    return settings


def test_walk_store_excludes_long_sequences(tmp_path):
    settings = _settings(tmp_path)
    project = settings.store_path / "project"
    project.mkdir(parents=True)
    for i in range(MAX_SEQUENCE_IMAGES + 1):
        (project / f"shot_{i:04d}_A.png").write_bytes(b"x")
    (project / "hero.png").write_bytes(b"x")
    (project / "keep_0001_A.png").write_bytes(b"x")

    found = _walk_store(settings)

    assert set(found) == {"project/hero.png", "project/keep_0001_A.png"}


def test_snapshot_inventory_excludes_long_sequences(tmp_path):
    settings = _settings(tmp_path)
    files = {
        f"project/shot_{i:04d}_A.png": {"mtime": 1.0, "size": 10}
        for i in range(MAX_SEQUENCE_IMAGES + 1)
    }
    files["project/hero.png"] = {"mtime": 1.0, "size": 10}
    settings.latest_store_snapshot_file.write_text(
        json.dumps({"version": 1, "files": files}), encoding="utf-8"
    )

    inventory = _load_snapshot_inventory(settings)

    assert inventory is not None
    assert set(inventory) == {"project/hero.png"}


def test_snapshot_of_only_long_sequences_falls_back_to_walk(tmp_path):
    settings = _settings(tmp_path)
    files = {
        f"project/shot_{i:04d}_A.png": {"mtime": 1.0, "size": 10}
        for i in range(MAX_SEQUENCE_IMAGES + 1)
    }
    settings.latest_store_snapshot_file.write_text(
        json.dumps({"version": 1, "files": files}), encoding="utf-8"
    )

    assert _load_snapshot_inventory(settings) is None
