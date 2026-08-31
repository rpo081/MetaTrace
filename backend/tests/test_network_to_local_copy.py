import io
import os

from network_copy_module import load_network_copy_module

mod = load_network_copy_module()


def _p(*parts):
    return os.path.join(*parts)


def test_filter_oversized_splits_by_snapshot_size():
    sizes = {
        _p("s", "big.tif"): 25 * 1024 * 1024,
        _p("s", "ok.png"): 5 * 1024 * 1024,
        _p("s", "unknown.jpg"): None,
    }
    paths = [_p("s", "big.tif"), _p("s", "ok.png"), _p("s", "unknown.jpg")]
    kept, too_large = mod.filter_oversized(paths, sizes, 20)
    assert kept == [_p("s", "ok.png"), _p("s", "unknown.jpg")]
    assert too_large == [_p("s", "big.tif")]


def test_filter_oversized_disabled_at_zero():
    kept, too_large = mod.filter_oversized(
        ["x"], {"x": 10**12}, 0
    )
    assert kept == ["x"] and too_large == []


def test_filter_images_drops_sequences_over_limit():
    def frames(n, name="frame"):
        return [
            _p("src", "proj", f"{name}{i:04d}.png") for i in range(n)
        ]

    long_seq = frames(150)          # dropped: > MAX_SEQUENCE_IMAGES
    ok_seq = frames(100, name="ok")  # kept: exactly at limit
    single = [_p("src", "proj", "hero.png")]  # unnumbered -> always kept
    all_paths = long_seq + ok_seq + single
    kept = mod.filter_images(all_paths, all_paths, "src")
    assert single[0] in kept
    assert ok_seq == [p for p in kept if p in set(ok_seq)]
    assert not any(p in set(long_seq) for p in kept)


def test_filter_images_respects_excluded_dir_names_within_level_depth():
    # EXCLUDED_DIR_LEVELS = 2: only the last two directory levels are checked.
    inside = _p("src", "proj", "textures", "a.png")       # last level -> excluded
    deep = _p("src", "textures", "deep", "very", "b.png")  # outside depth -> kept
    kept = mod.filter_images([inside, deep], [inside, deep], "src")
    assert inside not in kept and deep in kept


def test_extension_check_is_case_insensitive_and_splitext_based():
    for ext in (".png", ".PNG", ".Tif"):
        path = _p("src", "img" + ext)
        assert os.path.splitext(os.path.basename(path))[1].lower() in mod.EXTENSIONS


def test_render_copy_progress_reports_completed_jobs_and_files():
    line = mod.render_copy_progress(2, 4, 10, 40, 5)

    assert "[##########----------]  50%" in line
    assert "2/4 Verzeichnis-Jobs abgeschlossen" in line
    assert "10/40 Dateien in fertigen Jobs" in line
    assert "Laufzeit 5s" in line


def test_write_status_line_overwrites_previous_content():
    stream = io.StringIO()

    width = mod.write_status_line("Fortschritt 10%", stream=stream)
    width = mod.write_status_line("Fertig", previous_width=width, stream=stream)

    assert width == len("Fertig")
    assert stream.getvalue() == "\rFortschritt 10%\rFertig         "


def test_render_copy_dashboard_includes_job_summary():
    rows = mod.render_copy_dashboard("Fortschritt [#####]  25%", 3, 5, 61)

    assert rows[0] == "Kopiervorgang läuft ..."
    assert rows[1] == "Fortschritt [#####]  25%"
    assert "2/5 abgeschlossen" in rows[2]
    assert "1m 1s" in rows[2]


def test_render_copy_dashboard_can_include_target_path():
    rows = mod.render_copy_dashboard("Fortschritt [-----]   0%", 5, 5, 0, target_root="D:/dst")

    assert rows[0] == "Kopiervorgang läuft ..."
    assert rows[1] == "Ziel: D:/dst"
    assert rows[2] == "Fortschritt [-----]   0%"


def test_draw_progress_frame_clears_and_rewrites_screen():
    stream = io.StringIO()

    mod.draw_progress_frame(["Zeile 1", "Zeile 2"], stream=stream)

    assert stream.getvalue() == "\x1b[H\x1b[2J\x1b[HZeile 1\nZeile 2\n"


def test_build_copy_preview_rows_matches_cleanup_report_style():
    rows = mod.build_copy_preview_rows(
        r"\\nas\share",
        r"D:\mirror",
        12.4,
        1200,
        80,
        5,
        60,
        10,
        7,
        8,
        35,
        12 * 1024 * 1024,
    )

    assert rows[1] == "MetaTrace Network Copy Preview"
    assert rows[2] == "=" * 60
    assert "CHANGE SUMMARY" in rows
    assert "COPY FILTERS" in rows
    assert "COPY PLAN" in rows
    assert "Scanned:     1,200 files in 12.4s" in rows
    assert "To copy:     35 files (12.0 MiB)" in rows


def test_build_robocopy_command_omits_console_and_log_output_flags():
    command = mod.build_robocopy_command(
        _p("src", "proj"),
        _p("dst", "proj"),
        ["b.png", "a.png"],
    )

    assert "/TEE" not in command
    assert not any(part.startswith("/LOG:") for part in command)
    assert command[:3] == ["robocopy", _p("src", "proj"), _p("dst", "proj")]
    assert command[3:5] == ["a.png", "b.png"]
