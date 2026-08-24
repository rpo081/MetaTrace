from sync_menu_module import load_sync_menu_module

mod = load_sync_menu_module()


def test_settings_defaults_when_config_missing(tmp_path):
    settings = mod.Settings.load(tmp_path / "missing.json")
    assert settings.src == ""
    assert settings.dst == ""
    assert settings.mode == "final"
    assert settings.threads == 8
    assert settings.skip_dirs == []


def test_menu_includes_all_mode():
    assert mod.MODES == ("all", "final", "manual")
    assert "alle erlaubten Bilder" in mod.MODE_INFO["all"]


def test_settings_defaults_on_corrupt_json(tmp_path):
    config = tmp_path / "config.json"
    config.write_text("{not json", encoding="utf-8")
    settings = mod.Settings.load(config)
    assert settings.threads == 8 and settings.skip_dirs == []


def test_settings_roundtrip_preserves_values(tmp_path):
    config = tmp_path / "sub" / "config.json"
    settings = mod.Settings(
        src=r"\\server\share", dst=r"G:\images", mode="manual",
        threads=16, skip_dirs=["archive/old"],
    )
    settings.save(config)
    loaded = mod.Settings.load(config)
    assert loaded == settings


def test_settings_load_drops_unknown_fields_and_sanitizes(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(
        '{"mode": "nope", "threads": "24", "skip_dirs": "nope", "extra": 1}',
        encoding="utf-8",
    )
    settings = mod.Settings.load(config)
    assert settings.mode == "final"
    assert settings.threads == 24
    assert settings.skip_dirs == []


def test_clamp_threads_bounds():
    assert mod.clamp_threads(0) == 1
    assert mod.clamp_threads(999) == 128
    assert mod.clamp_threads(8) == 8


def test_clean_skip_entry_normalizes():
    assert mod.clean_skip_entry(" Archive\\Old ") == "Archive/Old"
    assert mod.clean_skip_entry("/lead/") == "lead"
    assert mod.clean_skip_entry("a/b") == "a/b"


def test_clean_skip_entry_rejects_invalid():
    assert mod.clean_skip_entry("") is None
    assert mod.clean_skip_entry("   ") is None
    assert mod.clean_skip_entry(".") is None
    assert mod.clean_skip_entry("./") is None


def test_shorten_collapses_long_paths_with_middle_ellipsis():
    short = r"G:\images"
    assert mod.shorten(short) == short
    long_path = "a" * 30 + "/" + "b" * 40
    collapsed = mod.shorten(long_path, 20)
    assert len(collapsed) == 20
    assert collapsed.startswith("a" * 9) and "…" in collapsed


def test_summary_rows_include_current_settings():
    settings = mod.Settings(src=r"\\server\share", dst=r"G:\images", mode="manual", threads=16)
    rows = mod.summary_rows(settings)
    assert any("server" in row for row in rows)
    assert any("manual" in row for row in rows)
    assert any("16" in row for row in rows)


def test_framed_rows_adds_top_and_bottom_borders():
    rows = mod.framed_rows(["source", "destination"], width=20)
    assert mod.ANSI_ESCAPE.sub("", rows[0]) == "░" * 20
    assert mod.ANSI_ESCAPE.sub("", rows[-1]) == "░" * 20
    assert rows[1].endswith("░\x1b[0m")
    assert rows[2].endswith("░\x1b[0m")
    assert len(mod.ANSI_ESCAPE.sub("", rows[1])) == 20
    assert len(mod.ANSI_ESCAPE.sub("", rows[2])) == 20


def test_framed_rows_wraps_folder_browser_header():
    rows = mod.framed_rows(["Quelle", "Pfad: C:/images"], width=30)
    assert all(len(mod.ANSI_ESCAPE.sub("", row)) == 30 for row in rows)


def test_framed_rows_clips_long_paths_inside_the_border():
    rows = mod.framed_rows([r"Quelle: \\server\share\very\long\folder\name"], width=30)
    assert all(len(mod.ANSI_ESCAPE.sub("", row)) == 30 for row in rows)
    assert "…" in mod.ANSI_ESCAPE.sub("", rows[1])


def test_alternate_screen_restores_primary_buffer(capsys):
    with mod.alternate_screen():
        pass
    output = capsys.readouterr().out
    assert "\x1b[?1049h" in output
    assert output.endswith("\x1b[?1049l")


def test_list_subdirectories_filters_and_sorts(tmp_path):
    (tmp_path / "beta").mkdir()
    (tmp_path / "Alpha").mkdir()
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "__MACOSX").mkdir()
    (tmp_path / "notes.txt").write_text("x")
    assert mod.list_subdirectories(tmp_path) == ["Alpha", "beta"]


def test_list_subdirectories_missing_dir_returns_empty(tmp_path):
    assert mod.list_subdirectories(tmp_path / "nope") == []


def test_is_accessible_directory_uses_longpath(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(mod.si, "longpath", lambda path: calls.append(path) or str(tmp_path))
    assert mod.is_accessible_directory(r"\\server\share")
    assert calls == [r"\\server\share"]
