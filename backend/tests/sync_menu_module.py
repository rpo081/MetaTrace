"""Load scripts/sync_menu.py as a module for testing."""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load_sync_menu_module():
    spec = importlib.util.spec_from_file_location("sync_menu", ROOT / "scripts" / "sync_menu.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["sync_images"] = load_sync_module()
    sys.modules["sync_menu"] = mod  # required before exec (dataclasses inspects it)
    spec.loader.exec_module(mod)
    return mod


def load_sync_module():
    from sync_module import load_sync_module as load

    return load()
