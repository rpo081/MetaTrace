import json
import io
import subprocess
import sys
from pathlib import Path

from network_menu_module import load_network_menu_module
from network_copy_module import load_network_copy_module

menu = load_network_menu_module()
nc = load_network_copy_module()


def test_archived_menu_imports_from_repo_root():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import runpy; "
                "runpy.run_path('scripts/archive/network_to_local_menu.py', "
                "run_name='not_main')"
            ),
        ],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_settings_defaults_include_size_limit():
    s = menu.Settings()
    assert s.max_file_size_mb == nc.MAX_FILE_SIZE_MB
    # Centralised via file_rules — value is now 100 (was 20 before centralisation)
    assert nc.MAX_FILE_SIZE_MB == 100


def test_settings_load_legacy_preset_without_size_key(tmp_path):
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({
        "active_preset": "Standard",
        "presets": {"Standard": {
            "src": r"\\x\y", "dst": r"D:\dst", "exclude_levels": 2,
            "exclude_paths": [], "exclude_dirs": [], "extensions": [".png"],
        }},
    }))
    s = menu.PresetConfig.load(cfg).current_settings()
    assert s.extensions == [".png"]
    assert s.max_file_size_mb == nc.MAX_FILE_SIZE_MB  # default applied for legacy configs


def test_normalize_rejects_invalid_size_values():
    for raw, expected in (("abc", nc.MAX_FILE_SIZE_MB), (-5, 0), (0, 0), ("35", 35)):
        s = menu.Settings(max_file_size_mb=raw)
        s.normalize()
        assert s.max_file_size_mb == expected, raw


def test_apply_settings_pushes_all_filters_into_copy_module():
    original = (nc.EXTENSIONS, nc.MAX_FILE_SIZE_MB)
    try:
        s = menu.Settings(
            exclude_levels=3,
            exclude_paths=["_tmp"],
            exclude_dirs=["textures"],
            extensions=[".jpg"],
            max_file_size_mb=50,
        )
        menu.apply_settings(s)
        assert nc.EXCLUDED_DIR_LEVELS == 3
        assert nc.EXCLUDED_SCAN_PATHS == {"_tmp"}
        assert nc.EXCLUDED_DIR_NAMES == {"textures"}
        assert nc.EXTENSIONS == {".jpg"}
        assert nc.MAX_FILE_SIZE_MB == 50
    finally:
        nc.EXTENSIONS, nc.MAX_FILE_SIZE_MB = original


def test_clean_extension_normalizes_and_rejects():
    assert menu.clean_extension("*.JPG") == ".jpg"
    assert menu.clean_extension("webp") == ".webp"
    assert menu.clean_extension(".a/b") is None
    assert menu.clean_extension("") is None


def test_run_flow_reports_runtime_error_and_pauses(monkeypatch, capsys, tmp_path):
    paused = {"called": False}
    settings = menu.Settings(src=r"\\x\y", dst=str(tmp_path), extensions=[".png"])

    monkeypatch.setattr(menu.sm, "is_accessible_directory", lambda path: True)
    monkeypatch.setattr(menu, "apply_settings", lambda settings: None)
    monkeypatch.setattr(menu.sm, "clear_screen", lambda: None)
    monkeypatch.setattr(menu.sm, "pause", lambda: paused.__setitem__("called", True))
    monkeypatch.setattr(menu.nc, "run", lambda src, dst: (_ for _ in ()).throw(RuntimeError("boom")))

    menu.run_flow(settings)

    out = capsys.readouterr().out
    assert "Fehler: boom" in out
    assert paused["called"] is True


class _DummyTTY(io.StringIO):
    def isatty(self):
        return True


def test_run_flow_suspends_alternate_screen_around_copy_output(monkeypatch, tmp_path):
    output = _DummyTTY()
    settings = menu.Settings(src=r"\\x\y", dst=str(tmp_path), extensions=[".png"])

    monkeypatch.setattr(menu.sm, "is_accessible_directory", lambda path: True)
    monkeypatch.setattr(menu, "apply_settings", lambda settings: None)
    monkeypatch.setattr(menu.sm, "pause", lambda: print("PAUSE", file=output))
    monkeypatch.setattr(menu.sys, "stdout", output)
    monkeypatch.setattr(menu.sm.sys, "stdout", output)
    monkeypatch.setattr(menu.nc, "run", lambda src, dst: print("PROMPT-REGION", file=output))

    menu.run_flow(settings)

    text = output.getvalue()
    assert "\x1b[?1049l" in text
    assert "\x1b[?1049h" in text
    assert text.index("\x1b[?1049l") < text.index("PROMPT-REGION")
    assert text.index("PROMPT-REGION") < text.index("PAUSE")
    assert text.index("PAUSE") < text.index("\x1b[?1049h")
