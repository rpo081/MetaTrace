from types import SimpleNamespace

from sync_module import load_sync_module

mod = load_sync_module()


def _touch(path, size=10):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


def test_folder_mode_matching_is_case_insensitive_and_component_based():
    assert mod.matches_mode(mod.Path("project/_FINAL/hero"), "final")
    assert mod.matches_mode(mod.Path("project/Manualbilder/output"), "manual")
    assert mod.matches_mode(mod.Path("project/MANUAL/output"), "manual")
    assert not mod.matches_mode(mod.Path("project/output"), "final")
    assert not mod.matches_mode(mod.Path("project/output"), "manual")


def test_final_mode_runs_robocopy_only_for_final_folders(tmp_path, monkeypatch):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _touch(src / "project" / "_final" / "render.png")
    _touch(src / "project" / "manual" / "guide.jpg")
    calls = []
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda command, check: calls.append(command) or SimpleNamespace(returncode=1),
    )

    stats = mod.Stats()
    mod.copy_matching_folders(src, dst, "final", 4, False, stats)
    assert len(calls) == 1
    assert "_final" in calls[0][1]
    assert "manual" not in calls[0][1]
    assert "/MT:4" in calls[0]
    assert f"/MAX:{mod.MAX_COPY_SIZE_BYTES - 1}" in calls[0]
    assert stats.folders == 1 and stats.failed == 0


def test_all_mode_runs_robocopy_for_every_image_folder(tmp_path, monkeypatch):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _touch(src / "render.png")
    _touch(src / "nested" / "detail.tif")
    _touch(src / "notes" / "readme.txt")
    calls = []
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda command, check: calls.append(command) or SimpleNamespace(returncode=0),
    )

    stats = mod.Stats()
    mod.copy_matching_folders(src, dst, "all", 8, False, stats)
    assert len(calls) == 2
    assert all("*.png" in command and "*.tif" in command for command in calls)
    assert stats.folders == 2 and stats.failed == 0


def test_manual_mode_runs_robocopy_for_manual_path_folders(tmp_path, monkeypatch):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _touch(src / "project" / "Manualbilder" / "hero" / "guide.jpeg")
    _touch(src / "project" / "_final" / "render.png")
    calls = []
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda command, check: calls.append(command) or SimpleNamespace(returncode=0),
    )

    stats = mod.Stats()
    mod.copy_matching_folders(src, dst, "manual", 8, False, stats)
    assert len(calls) == 1
    assert "Manualbilder" in calls[0][1]
    assert "_final" not in calls[0][1]
    assert stats.folders == 1 and stats.failed == 0


def test_manual_mode_includes_images_below_manual_named_source_root(tmp_path, monkeypatch):
    src = tmp_path / "MR_Manual"
    dst = tmp_path / "dst"
    _touch(src / "hero.png")
    _touch(src / "nested" / "detail.tif")
    calls = []
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda command, check: calls.append(command) or SimpleNamespace(returncode=0),
    )

    stats = mod.Stats()
    mod.copy_matching_folders(src, dst, "manual", 8, False, stats)
    assert len(calls) == 2
    assert all("*.png" in command and "*.tif" in command for command in calls)
    assert all("/LEV:1" in command for command in calls)
    assert stats.folders == 2 and stats.failed == 0


def test_manual_mode_copies_only_manual_named_images_outside_manual_paths(tmp_path, monkeypatch):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _touch(src / "project" / "output" / "hero_manual.png")
    _touch(src / "project" / "output" / "hero.png")
    calls = []
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda command, check: calls.append(command) or SimpleNamespace(returncode=0),
    )

    stats = mod.Stats()
    mod.copy_matching_folders(src, dst, "manual", 8, False, stats)
    assert len(calls) == 1
    assert "*manual*.png" in calls[0]
    assert "*.png" not in calls[0]
    assert stats.folders == 1 and stats.failed == 0


def test_manual_mode_accepts_manual_named_tif_files(tmp_path, monkeypatch):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _touch(src / "project" / "output" / "hero_manual.tif")
    calls = []
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda command, check: calls.append(command) or SimpleNamespace(returncode=0),
    )

    stats = mod.Stats()
    mod.copy_matching_folders(src, dst, "manual", 8, False, stats)
    assert len(calls) == 1
    assert "*manual*.tif" in calls[0]
    assert stats.folders == 1 and stats.failed == 0


def test_skip_dir_prunes_source_subfolder(tmp_path, monkeypatch):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _touch(src / "keep" / "_final" / "render.png")
    _touch(src / "archive" / "old" / "_final" / "skip.png")
    calls = []
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda command, check: calls.append(command) or SimpleNamespace(returncode=0),
    )

    stats = mod.Stats()
    skips = mod.normalize_skip_dirs(["archive\\old"])
    mod.copy_matching_folders(src, dst, "final", 8, False, stats, skips)
    assert len(calls) == 1
    assert "keep" in calls[0][1]
    assert "archive" not in calls[0][1]


def test_robocopy_failures_are_reported(tmp_path, monkeypatch):
    src = tmp_path / "src"
    _touch(src / "project" / "_final" / "render.png")
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda command, check: SimpleNamespace(returncode=8),
    )

    stats = mod.Stats()
    mod.copy_matching_folders(src, tmp_path / "dst", "final", 8, False, stats)
    assert stats.folders == 1 and stats.failed == 1
    assert "exit code 8" in stats.errors[0]
