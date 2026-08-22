#!/usr/bin/env python3
"""Mirror image files (.png/.jpg/.jpeg/.psd) from a source folder (e.g. a CIFS
network share) to a local destination.

Recursive: every subfolder below the root is scanned and its relative structure
rebuilt at the destination. Only image files take part in the sync; other files
are ignored on the source side (and deleted from the destination by --prune).
Destination folders that become empty through pruning — including previously
orphaned ones — are removed.

Incremental: files are compared by (size, mtime); only new or changed files are
copied, unchanged files are skipped. Designed for Windows (CIFS drive letter or
UNC paths, long-path safe) but works on POSIX as well.

How to operate:
    * First run against an empty destination copies every image (full mirror).
    * Every later run transfers only new/changed files and is safe to re-run
      or interrupt at any time — the next run simply finishes the job.
    * Run it before starting the MetaTrace scan/index step so the indexer sees
      a consistent local store, and schedule it (e.g. Windows Task Scheduler
      nightly, see README.md) to keep the SSD copy current.
    * Use --dry-run first when pointing the script at a new destination; it
      prints the planned copies/deletes without touching anything.

Usage:
    python sync_images.py SRC DST [--prune] [--dry-run] [--threads N]

Examples:
    python sync_images.py "\\\\nas\\share\\renderings" "D:\\imagestore" --prune
    python sync_images.py /mnt/nas/renderings ./local_store --dry-run

Exit codes:
    0 = success, 1 = usage/argument error, 2 = completed with failures.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

IS_WINDOWS = os.name == "nt"

# Junk directories commonly found on network shares that must never be synced.
SKIP_DIRS = {
    "$RECYCLE.BIN",
    "System Volume Information",
    ".Spotlight-V100",
    ".Trashes",
    "__MACOSX",
}

# Only these image formats take part in the sync; everything else is ignored.
ALLOWED_EXTS = frozenset({".png", ".jpg", ".jpeg", ".psd"})

# CIFS/FAT filesystems often store timestamps with coarse resolution; treat
# small mtime differences as "unchanged" to avoid pointless re-copies.
MTIME_TOLERANCE_SEC = 2.0


def longpath(path: str | os.PathLike[str]) -> str:
    """Return an absolute Windows long-path-safe string ('\\\\?\\' prefix).

    Windows limits regular paths to MAX_PATH (260) characters. Deep render
    folder hierarchies on a network share regularly exceed that, so every file
    access in this script goes through this helper: on Windows the '\\\\?\\'
    prefix tells the Win32 API to skip path parsing/normalization and accept up
    to ~32k characters. UNC network paths get the special '\\\\?\\UNC\\' form so
    the prefix works for shares too ("\\\\server\\share\\dir" becomes
    "\\\\?\\UNC\\server\\share\\dir"). Paths already carrying the prefix are left
    untouched (double-prefixing would break them).

    On POSIX the path is returned as an absolute string unchanged — os.path
    there has no practical length limit.
    """
    p = os.path.abspath(os.fspath(path))
    if not IS_WINDOWS or p.startswith("\\\\?\\"):
        return p
    if p.startswith("\\\\"):  # UNC -> \\?\UNC\server\share\...
        return "\\\\?\\UNC\\" + p.lstrip("\\")
    return "\\\\?\\" + p


@dataclass
class Action:
    """One planned operation produced by plan_sync().

    rel  : POSIX-style path ('sub/dir/file.png') relative to the sync root;
           forward slashes keep the value portable between OSes and stable in
           logs/tests. Joined onto src/dst roots only at execution time.
    kind : "copy"   -> transfer from source to destination,
           "skip"   -> unchanged, leave as is,
           "delete" -> exists only at the destination (requires --prune).
    size : file size in bytes, used for progress reporting and byte totals.
    """

    rel: str  # POSIX-style path relative to the sync root
    kind: str  # "copy" | "skip" | "delete"
    size: int = 0


@dataclass
class Stats:
    """Aggregated counters for one sync run.

    A single instance travels through scanning and execution and is shared
    with the copy worker threads; CPython's GIL makes the plain int increments
    safe enough for statistics purposes. `errors` collects one short message
    per failed operation so problems can be reviewed after long unattended
    runs without watching the log.
    """

    scanned: int = 0          # image files found on the source
    copied: int = 0           # files transferred (or planned, in dry-run)
    skipped: int = 0          # files already present and unchanged
    deleted: int = 0          # destination-only files removed (--prune)
    dirs_removed: int = 0     # directories that became empty after pruning
    failed: int = 0           # operations that raised OSError
    bytes_copied: int = 0     # total payload volume, for the summary line
    errors: list[str] = field(default_factory=list)


def scan_tree(
    root: Path, stats: Stats, images_only: bool = True
) -> dict[str, tuple[int, float]]:
    """Return {posix_rel_path: (size, mtime)} for every regular file under root.

    This is the recursive inventory of one side of the sync:

    * os.walk descends through the whole tree starting at `root`; junk
      directories (SKIP_DIRS) and hidden dot-directories are removed from
      `dirnames` in place, which both prevents descending into them and keeps
      them out of the results.
    * Children are visited in sorted order so the output — and therefore the
      verbose log and all tests — is deterministic regardless of filesystem
      enumeration order.
    * With images_only=True, files whose extension (compared case-insensitively)
      is not in ALLOWED_EXTS are ignored. The source is always scanned with the
      filter; the destination is scanned with images_only=False so stray
      non-image files from earlier runs stay visible to the planner and can be
      removed by --prune.
    * Files whose stat() fails (e.g. permission or transient network errors on
      a CIFS share) are counted as failures and omitted; they will be treated
      as new or stale on the next run instead of aborting the whole sync.

    The returned mapping is the complete input for plan_sync(); nothing else
    about the tree is needed because "changed" is defined purely as a change
    in size or mtime beyond MTIME_TOLERANCE_SEC.
    """
    base = Path(longpath(root))
    out: dict[str, tuple[int, float]] = {}
    for dirpath, dirnames, filenames in os.walk(base):
        # In-place filter: mutating dirnames makes os.walk skip pruned dirs.
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        dp = Path(dirpath)
        for name in sorted(filenames):
            ap = dp / name
            if images_only and ap.suffix.lower() not in ALLOWED_EXTS:
                continue
            rel = ap.relative_to(base).as_posix()
            try:
                st = os.stat(ap)
            except OSError as exc:
                stats.failed += 1
                stats.errors.append(f"stat {rel}: {exc}")
                continue
            out[rel] = (st.st_size, st.st_mtime)
    return out


def plan_sync(
    src_files: dict[str, tuple[int, float]],
    dst_files: dict[str, tuple[int, float]],
    prune: bool,
) -> list[Action]:
    """Compute the action list that turns dst into a mirror of src.

    Pure function: no I/O, trivially testable. For every source file:

    * missing at the destination            -> copy (new),
    * different size, or mtime differing by more than MTIME_TOLERANCE_SEC
      (CIFS shares often have coarse 1–2 s timestamp resolution, hence the
      tolerance)                           -> copy (changed),
    * otherwise                             -> skip.

    With prune=True, files that exist only in dst_files (deleted on the source,
    renamed, or non-images caught by the destination's unfiltered scan) get a
    delete action. Actions are emitted sorted by rel path for readable logs.
    """
    actions: list[Action] = []
    for rel, (size, mtime) in sorted(src_files.items()):
        d = dst_files.get(rel)
        if d is None:
            actions.append(Action(rel, "copy", size))
        elif d[0] != size or abs(d[1] - mtime) > MTIME_TOLERANCE_SEC:
            actions.append(Action(rel, "copy", size))
        else:
            actions.append(Action(rel, "skip", size))
    if prune:
        for rel in sorted(set(dst_files) - set(src_files)):
            actions.append(Action(rel, "delete", dst_files[rel][0]))
    return actions


def _copy_one(src_root: Path, dst_root: Path, action: Action, stats: Stats) -> None:
    """Transfer a single file; runs inside the ThreadPoolExecutor workers.

    The parent directory is created on demand with parents=True, which is how
    the source hierarchy gets rebuilt at the destination — and why the sync
    never produces empty folders: a directory only comes into existence as the
    parent of an actual image file.

    shutil.copy2 (not copyfile/copy) preserves mtime while copying. That is
    essential for correctness, not convenience: plan_sync() compares mtimes to
    detect changes, so a copy that lost its timestamp would be re-transferred
    on every run.

    Failures (network drop, disk full, path issues) are recorded in `stats`
    instead of raising, so one bad file neither aborts the remaining copies
    nor kills the worker pool.
    """
    sp = Path(longpath(src_root)) / Path(action.rel)
    dp = Path(longpath(dst_root)) / Path(action.rel)
    try:
        dp.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(sp, dp)
        stats.copied += 1
        stats.bytes_copied += action.size
    except OSError as exc:
        stats.failed += 1
        stats.errors.append(f"copy {action.rel}: {exc}")


def _delete_one(dst_root: Path, action: Action, stats: Stats) -> None:
    """Remove one destination-only file (only called with --prune)."""
    dp = Path(longpath(dst_root)) / Path(action.rel)
    try:
        dp.unlink()
        stats.deleted += 1
    except OSError as exc:
        stats.failed += 1
        stats.errors.append(f"delete {action.rel}: {exc}")


def _remove_empty_dirs(dst_root: Path, stats: Stats) -> None:
    """Prune directories left empty by deletions (including older orphans).

    os.walk(topdown=False) visits children before their parents, so by the
    time a parent is examined its already-removed children are gone from disk
    and it can be removed too if nothing else remains — empty chains like
    stale/deep/ collapse completely in one pass.

    rmdir only succeeds on truly empty directories; the resulting OSError is
    swallowed deliberately, meaning "still has content (or is unreadable)" —
    such directories are kept. The destination root itself never qualifies
    because deleting the sync target would break the MetaTrace store.
    """
    base = Path(longpath(dst_root))
    for dirpath, _dirnames, _filenames in os.walk(base, topdown=False):
        d = Path(dirpath)
        if d == base:
            continue
        try:
            d.rmdir()
            stats.dirs_removed += 1
        except OSError:
            pass  # not empty (or unreadable) — keep it


def execute(
    src: Path,
    dst: Path,
    actions: list[Action],
    threads: int,
    dry_run: bool,
    verbose: bool,
    stats: Stats,
    prune: bool = False,
) -> None:
    """Carry out the planned actions; the only function that touches the disk.

    Three phases:

    1. Copies run in parallel (ThreadPoolExecutor with `threads` workers,
       clamped to at least 1). Copies dominate the runtime — network latency
       and SSD writes overlap nicely — so they get the pool. The results are
       consumed in submission order to keep verbose output deterministic.
    2. Deletes run serially afterwards: they are cheap metadata operations
       where parallelism buys nothing, and this ordering guarantees no delete
       can race with a copy of a file at the same path.
    3. With prune=True (and never in dry-run), _remove_empty_dirs() cleans up
       directories emptied by deletions.

    With dry_run=True nothing is created, modified or removed; planned copies
    and deletes are only printed and counted into `stats`, which is why the
    summary can honestly say what "would" happen. `verbose` additionally logs
    one line per action ("[i/total] kind rel").
    """
    copies = [a for a in actions if a.kind == "copy"]
    deletes = [a for a in actions if a.kind == "delete"]

    def report(i: int, total: int, a: Action) -> None:
        if verbose:
            print(f"[{i}/{total}] {a.kind} {a.rel}")

    done = 0
    total = len(copies) + len(deletes)
    if dry_run:
        for a in copies:
            done += 1
            report(done, total, a)
        stats.copied = len(copies)
        stats.bytes_copied = sum(a.size for a in copies)
        for a in deletes:
            done += 1
            report(done, total, a)
        stats.deleted = len(deletes)
        return

    with ThreadPoolExecutor(max_workers=max(1, threads)) as pool:
        futures = [pool.submit(_copy_one, src, dst, a, stats) for a in copies]
        for fut, a in zip(futures, copies):
            fut.result()
            done += 1
            report(done, total, a)
        for a in deletes:
            _delete_one(dst, a, stats)
            done += 1
            report(done, total, a)
        if prune:
            _remove_empty_dirs(dst, stats)


def print_summary(stats: Stats, actions: list[Action], dry_run: bool) -> None:
    """Print the end-of-run report.

    In dry-run mode the copied/deleted counts describe the *plan*, hence the
    "would be" wording. At most 20 collected errors are shown with pointers to
    the affected files — enough to diagnose a flaky share without flooding the
    console on a 20k-file initial sync.
    """
    verb = "would be" if dry_run else ""
    print("-" * 60)
    print(f"scanned : {stats.scanned} source files")
    print(f"copied  : {stats.copied} {verb}".rstrip())
    print(f"skipped : {stats.skipped}")
    print(f"deleted : {stats.deleted} {verb}".rstrip())
    if stats.dirs_removed:
        print(f"dirs    : {stats.dirs_removed} removed")
    print(f"bytes   : {stats.bytes_copied:,}")
    print(f"failed  : {stats.failed}")
    for err in stats.errors[:20]:
        print(f"  ERROR {err}")
    if len(stats.errors) > 20:
        print(f"  ... and {len(stats.errors) - 20} more")
    if not actions:
        print("already in sync.")


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; returns the process exit code.

    Pipeline: validate arguments -> inventory source (images only) ->
    validate/create destination -> inventory destination (unfiltered, so stray
    non-images are visible to --prune) -> plan -> execute -> summarize.

    Exit codes: 0 success, 1 argument/validation error (nothing was synced),
    2 the run finished but at least one operation failed — check the ERROR
    lines in the summary; a follow-up run retries exactly those files.
    """
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("src", help="source folder (network share)")
    parser.add_argument("dst", help="destination folder (local SSD)")
    parser.add_argument("--prune", action="store_true",
                        help="delete destination files that no longer exist in the source")
    parser.add_argument("--dry-run", action="store_true", help="show what would happen, change nothing")
    parser.add_argument("--threads", type=int, default=8, help="parallel copy workers (default: 8)")
    parser.add_argument("--verbose", "-v", action="store_true", help="log every file action")
    args = parser.parse_args(argv)

    src = Path(args.src)
    dst = Path(args.dst)
    if not src.is_dir():
        print(f"error: source is not an accessible directory: {src}", file=sys.stderr)
        return 1

    started = time.time()
    stats = Stats()
    src_files = scan_tree(src, stats)
    stats.scanned = len(src_files)

    # A file at the destination path (not a directory) would break every copy;
    # refuse early. The directory itself is created only for real runs so a
    # dry-run against a not-yet-existing destination changes nothing.
    if dst.exists() and not dst.is_dir():
        print(f"error: destination exists and is not a directory: {dst}", file=sys.stderr)
        return 1
    if not args.dry_run:
        dst.mkdir(parents=True, exist_ok=True)

    dst_stats = Stats()
    dst_files = scan_tree(dst, dst_stats, images_only=False) if dst.exists() else {}

    actions = plan_sync(src_files, dst_files, args.prune)
    stats.skipped = sum(1 for a in actions if a.kind == "skip")
    execute(src, dst, actions, args.threads, args.dry_run, args.verbose, stats,
            prune=args.prune)
    stats.failed += dst_stats.failed  # propagate destination scan errors

    print_summary(stats, actions, args.dry_run)
    print(f"done in {time.time() - started:.1f}s")
    return 2 if stats.failed else 0


if __name__ == "__main__":
    sys.exit(main())
