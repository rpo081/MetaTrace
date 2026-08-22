"""Load scripts/sync_images.py as a module for testing."""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load_sync_module():
    spec = importlib.util.spec_from_file_location("sync_images", ROOT / "scripts" / "sync_images.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["sync_images"] = mod  # required before exec (dataclasses inspects it)
    spec.loader.exec_module(mod)
    return mod
