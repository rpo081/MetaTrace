"""Load scripts/network_to_local_menu.py as a module for testing."""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load_network_menu_module():
    cached = sys.modules.get("network_to_local_menu")
    if cached is not None:
        return cached
    # The menu imports its siblings by name; register them first so the
    # exec below reuses the same module objects.
    if "network_to_local_copy" not in sys.modules:
        from network_copy_module import load_network_copy_module

        load_network_copy_module()
    spec = importlib.util.spec_from_file_location(
        "network_to_local_menu", ROOT / "scripts" / "network_to_local_menu.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["network_to_local_menu"] = mod  # required before exec (dataclasses inspects it)
    spec.loader.exec_module(mod)
    return mod