import importlib.util
import sys


def _load_store_snapshot_module():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "store_snapshot_script", root / "scripts" / "store_snapshot.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["store_snapshot_script"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_default_root_path_uses_repo_env_local_image_store(tmp_path, monkeypatch):
    mod = _load_store_snapshot_module()
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
    monkeypatch.delenv("STORE_SNAPSHOT_ROOT_PATH", raising=False)
    (tmp_path / ".env").write_text("LOCAL_IMAGE_STORE=G:\\\n", encoding="utf-8")

    assert mod.default_root_path() == "G:\\"


def test_default_root_path_prefers_explicit_override(tmp_path, monkeypatch):
    mod = _load_store_snapshot_module()
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
    monkeypatch.setenv("STORE_SNAPSHOT_ROOT_PATH", "H:\\override")
    (tmp_path / ".env").write_text("LOCAL_IMAGE_STORE=G:\\\n", encoding="utf-8")

    assert mod.default_root_path() == "H:\\override"