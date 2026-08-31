"""Load scripts/network_to_local_copy.py as a module for testing."""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load_network_copy_module():
    cached = sys.modules.get("network_to_local_copy")
    if cached is not None:
        return cached
    candidate = ROOT / "scripts" / "network_to_local_copy.py"
    if not candidate.exists():
        candidate = ROOT / "scripts" / "archive" / "network_to_local_copy.py"
    spec = importlib.util.spec_from_file_location(
        "network_to_local_copy", candidate
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["network_to_local_copy"] = mod  # required before exec (dataclasses inspects it)
    spec.loader.exec_module(mod)
    return mod
