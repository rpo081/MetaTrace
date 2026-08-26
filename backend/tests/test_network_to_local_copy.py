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
