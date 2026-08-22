from sync_module import load_sync_module

mod = load_sync_module()


def _touch(path, size=10):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


def test_longpath_passthrough_posix():
    assert mod.longpath("/tmp/x") == "/tmp/x"


def test_new_and_unchanged_and_changed(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _touch(src / "a.png", 5)
    _touch(src / "sub" / "b.jpg", 7)

    stats = mod.Stats()
    plan = mod.plan_sync(mod.scan_tree(src, stats), {}, prune=False)
    kinds = {a.rel: a.kind for a in plan}
    assert kinds == {"a.png": "copy", "sub/b.jpg": "copy"}

    # first execution copies everything
    ex_stats = mod.Stats()
    actions = mod.plan_sync(
        mod.scan_tree(src, ex_stats), mod.scan_tree(dst, ex_stats), False
    )
    mod.execute(src, dst, actions, threads=4, dry_run=False, verbose=False,
                stats=ex_stats)
    assert ex_stats.copied == 2 and ex_stats.failed == 0
    assert (dst / "a.png").stat().st_size == 5

    # second run: nothing to do
    s2 = mod.Stats()
    actions = mod.plan_sync(mod.scan_tree(src, s2), mod.scan_tree(dst, s2), False)
    assert all(a.kind == "skip" for a in actions)

    # changed content (size differs) is re-copied
    _touch(src / "a.png", 9)
    s3 = mod.Stats()
    actions = mod.plan_sync(mod.scan_tree(src, s3), mod.scan_tree(dst, s3), False)
    assert [a.rel for a in actions if a.kind == "copy"] == ["a.png"]


def test_prune_removes_dst_only_files(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _touch(src / "keep.png")
    _touch(dst / "stale.png")
    _touch(dst / "sub" / "gone.jpg")

    s = mod.Stats()
    actions = mod.plan_sync(
        mod.scan_tree(src, s), mod.scan_tree(dst, s), prune=True
    )
    mod.execute(src, dst, actions, 4, False, False, s)
    assert (dst / "keep.png").exists()
    assert not (dst / "stale.png").exists()
    assert not (dst / "sub" / "gone.jpg").exists()


def test_dry_run_changes_nothing(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _touch(src / "a.png")
    _touch(dst / "old.png")

    s = mod.Stats()
    actions = mod.plan_sync(
        mod.scan_tree(src, s), mod.scan_tree(dst, s), prune=True
    )
    mod.execute(src, dst, actions, 4, dry_run=True, verbose=False, stats=s)
    assert not (dst / "a.png").exists()      # nothing copied
    assert (dst / "old.png").exists()        # nothing deleted
    assert s.copied == 1 and s.deleted == 1  # but reported


def test_verbose_reports_each_file_once(tmp_path, capsys):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _touch(src / "a.png")
    _touch(src / "b.jpg")

    s = mod.Stats()
    actions = mod.plan_sync(
        mod.scan_tree(src, s), mod.scan_tree(dst, s), prune=False
    )
    mod.execute(src, dst, actions, 4, False, True, s)
    out = capsys.readouterr().out
    assert out.count("copy a.png") == 1
    assert out.count("copy b.jpg") == 1


def test_skip_dirs(tmp_path):
    src = tmp_path / "src"
    _touch(src / "$RECYCLE.BIN" / "junk")
    _touch(src / ".hidden" / "x.png")
    _touch(src / "ok.png")
    s = mod.Stats()
    files = mod.scan_tree(src, s)
    assert list(files) == ["ok.png"]


def test_only_images_scanned_and_copied(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _touch(src / "a.png")
    _touch(src / "b.txt")
    _touch(src / "c.PNG")
    _touch(src / "d.jpeg")
    _touch(src / "sub" / "e.psd")
    _touch(src / "sub" / "f.gif")

    files = mod.scan_tree(src, mod.Stats())
    assert set(files) == {"a.png", "c.PNG", "d.jpeg", "sub/e.psd"}

    actions = mod.plan_sync(files, {}, False)
    mod.execute(src, dst, actions, 4, False, False, mod.Stats())
    got = {p.relative_to(dst).as_posix() for p in dst.rglob("*") if p.is_file()}
    assert got == {"a.png", "c.PNG", "d.jpeg", "sub/e.psd"}


def test_prune_removes_stray_non_image_on_dst(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _touch(src / "a.png")
    _touch(dst / "a.png")
    _touch(dst / "notes.txt")
    _touch(dst / "docs" / "readme.md")

    s = mod.Stats()
    actions = mod.plan_sync(
        mod.scan_tree(src, s), mod.scan_tree(dst, s, images_only=False), True
    )
    mod.execute(src, dst, actions, 4, False, False, s, prune=True)
    assert (dst / "a.png").exists()
    assert not (dst / "notes.txt").exists()
    assert not (dst / "docs").exists()  # emptied by prune


def test_prune_removes_empty_dirs(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _touch(src / "keep.png")
    _touch(dst / "keep.png")
    _touch(dst / "stale" / "deep" / "x.jpg")
    (dst / "orphan").mkdir()  # empty from an earlier run

    s = mod.Stats()
    actions = mod.plan_sync(
        mod.scan_tree(src, s), mod.scan_tree(dst, s), True
    )
    mod.execute(src, dst, actions, 4, False, False, s, prune=True)
    assert (dst / "keep.png").exists()
    assert not (dst / "stale").exists()
    assert not (dst / "orphan").exists()
    assert s.dirs_removed == 3
