from backend.app.file_rules import (
    MAX_SEQUENCE_IMAGES,
    filter_long_sequences,
    sequence_key,
)


def test_sequence_key_matches_trailing_digits():
    assert sequence_key("proj/frame0001.png") == ("proj", "frame#", ".png")


def test_sequence_key_matches_embedded_number_with_suffix():
    assert sequence_key("proj/project_0001_A.png") == ("proj", "project_#_a", ".png")


def test_sequence_key_groups_numbered_siblings_and_keeps_suffixes_apart():
    a1 = sequence_key("proj/project_0001_A.png")
    a2 = sequence_key("proj/project_0002_A.png")
    b1 = sequence_key("proj/project_0001_B.png")

    assert a1 == a2
    assert a1 != b1


def test_sequence_key_normalizes_every_number_run():
    assert sequence_key("proj/scene_001_take2.png") == ("proj", "scene_#_take#", ".png")


def test_sequence_key_is_case_insensitive_and_separates_dirs_and_extensions():
    assert sequence_key("Proj/Shot_0001.PNG") == sequence_key("proj/shot_0010.png")
    assert sequence_key("proj/shot_0001.png") != sequence_key("other/shot_0001.png")
    assert sequence_key("proj/shot_0001.png") != sequence_key("proj/shot_0001.jpg")


def test_sequence_key_ignores_unnumbered_and_pure_digit_names():
    assert sequence_key("proj/hero.png") is None
    assert sequence_key("proj/0001.png") is None
    assert sequence_key("proj/0001_0002.png") is None


def test_filter_long_sequences_drops_only_over_limit_groups():
    long_seq = [f"proj/project_{i:04d}_A.png" for i in range(MAX_SEQUENCE_IMAGES + 1)]
    short_seq = [f"proj/other_{i:04d}_A.png" for i in range(MAX_SEQUENCE_IMAGES)]
    single = ["proj/hero.png"]

    dropped = filter_long_sequences(long_seq + short_seq + single)

    assert dropped == set(long_seq)


def test_filter_long_sequences_keeps_group_exactly_at_limit():
    paths = [f"proj/frame{i:04d}.png" for i in range(MAX_SEQUENCE_IMAGES)]

    assert filter_long_sequences(paths) == set()
