#!/usr/bin/env python3
"""Copy selected image folders from a Windows share with robocopy.

Only PNG, JPG, JPEG, TIF, and TIFF files smaller than 20 MiB are copied. Python
traverses the source tree; robocopy performs each folder transfer using native
workers.

Usage:
    python sync_images.py SRC DST --mode {final,manual} [--threads N] [--skip-dir PATH] [--dry-run]

Examples:
    python sync_images.py "\\\\server\\share" "G:\\images" --mode final --threads 8
    python sync_images.py "\\\\server\\share" "G:\\images" --mode manual --threads 8
    python sync_images.py "\\\\server\\share" "G:\\images" --mode manual --skip-dir "archive/old"
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

IS_WINDOWS = os.name == "nt"
ALLOWED_EXTS = frozenset({".png", ".jpg", ".jpeg", ".tif", ".tiff"})
MAX_COPY_SIZE_BYTES = 20 * 1024 * 1024
SKIP_DIRS = frozenset({
    "$RECYCLE.BIN",
    "System Volume Information",
    ".Spotlight-V100",
    ".Trashes",
    "__MACOSX",
})


def longpath(path: str | os.PathLike[str]) -> str:
    """Return an absolute Windows extended path for Python file operations."""
    value = os.path.abspath(os.fspath(path))
    if not IS_WINDOWS or value.startswith("\\\\?\\"):
        return value
    if value.startswith("\\\\"):
        return "\\\\?\\UNC\\" + value.lstrip("\\")
    return "\\\\?\\" + value


def robocopy_path(path: str | os.PathLike[str]) -> str:
    """Return a normal path because robocopy rejects Windows extended paths."""
    value = os.fspath(path)
    if value.startswith("\\\\?\\UNC\\"):
        return "\\\\" + value[8:]
    if value.startswith("\\\\?\\"):
        return value[4:]
    return value


@dataclass
class Stats:
    folders: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


def matches_mode(relative_dir: Path, mode: str) -> bool:
    """Return whether a folder name in the path contains the selected mode."""
    return any(mode in part.casefold() for part in relative_dir.parts)


def image_masks(mode: str, path_matches: bool) -> tuple[str, ...]:
    """Return robocopy image masks for the selected folder."""
    if mode == "manual" and not path_matches:
        return (
            "*manual*.png", "*manual*.jpg", "*manual*.jpeg",
            "*manual*.tif", "*manual*.tiff",
        )
    return ("*.png", "*.jpg", "*.jpeg", "*.tif", "*.tiff")


def normalize_skip_dirs(skip_dirs: list[str]) -> frozenset[str]:
    """Return case-insensitive, source-relative paths for walk pruning."""
    posix_names = [path.replace("\\", "/") for path in skip_dirs]
    normalized = frozenset(
        Path(posix_name).as_posix().strip("/").casefold()
        for posix_name in posix_names
        if Path(posix_name).as_posix().strip("/")
    )
    if "." in normalized:
        raise ValueError("--skip-dir must name a subfolder below SRC")
    return normalized


def copy_matching_folders(
    src: Path,
    dst: Path,
    mode: str,
    threads: int,
    dry_run: bool,
    stats: Stats,
    skip_dirs: frozenset[str] = frozenset(),
) -> None:
    """Run robocopy for every matching source directory containing images."""
    source_base = Path(longpath(src))
    destination_base = Path(longpath(dst))
    source_path_matches = matches_mode(src, mode)
    previous_folder: str | None = None

    for dirpath, dirnames, filenames in os.walk(source_base):
        current_relative = Path(dirpath).relative_to(source_base)
        dirnames[:] = sorted(
            name for name in dirnames
            if name not in SKIP_DIRS
            and not name.startswith(".")
            and (current_relative / name).as_posix().casefold() not in skip_dirs
        )
        source_dir = Path(dirpath)
        relative_dir = current_relative
        path_matches = source_path_matches or matches_mode(relative_dir, mode)
        filename_matches = (
            mode == "manual"
            and any(
                "manual" in Path(name).stem.casefold()
                and Path(name).suffix.casefold() in ALLOWED_EXTS
                for name in filenames
            )
        )
        if not path_matches and not filename_matches:
            continue
        if not any(Path(name).suffix.casefold() in ALLOWED_EXTS for name in filenames):
            continue

        folder = relative_dir.as_posix()
        if folder != previous_folder:
            print(f"\rcopying folder: {folder}", end="", flush=True)
            previous_folder = folder

        command = [
            "robocopy",
            robocopy_path(source_dir),
            robocopy_path(destination_base / relative_dir),
            *image_masks(mode, path_matches),
            "/LEV:1",
            f"/MAX:{MAX_COPY_SIZE_BYTES - 1}",
            f"/MT:{max(1, min(128, threads))}",
            "/R:1",
            "/W:1",
            "/COPY:DAT",
            "/DCOPY:T",
            "/FFT",
            "/NP",
            "/NFL",
            "/NDL",
            "/NJH",
            "/NJS",
        ]
        if dry_run:
            command.append("/L")

        try:
            result = subprocess.run(command, check=False)
        except OSError as exc:
            stats.failed += 1
            stats.errors.append(f"robocopy {folder}: {exc}")
            continue

        stats.folders += 1
        if result.returncode >= 8:
            stats.failed += 1
            stats.errors.append(f"robocopy {folder}: exit code {result.returncode}")

    if previous_folder is not None:
        print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("src", help="source folder (network share)")
    parser.add_argument("dst", help="destination folder")
    parser.add_argument(
        "--mode",
        choices=("final", "manual"),
        required=True,
        help="copy matching images below folders whose names contain this value",
    )
    parser.add_argument(
        "--threads", type=int, default=8, help="robocopy worker threads (default: 8)"
    )
    parser.add_argument(
        "--skip-dir",
        action="append",
        default=[],
        metavar="PATH",
        help="source-relative subfolder to skip; may be specified more than once",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="show robocopy actions without copying"
    )
    args = parser.parse_args(argv)

    if not IS_WINDOWS:
        parser.error("this robocopy-based script requires Windows")

    src = Path(args.src)
    dst = Path(args.dst)
    if not src.is_dir():
        print(f"error: source is not an accessible directory: {src}", file=sys.stderr)
        return 1
    if dst.exists() and not dst.is_dir():
        print(f"error: destination exists and is not a directory: {dst}", file=sys.stderr)
        return 1
    if not args.dry_run:
        dst.mkdir(parents=True, exist_ok=True)

    started = time.time()
    stats = Stats()
    try:
        skip_dirs = normalize_skip_dirs(args.skip_dir)
    except ValueError as exc:
        parser.error(str(exc))
    copy_matching_folders(
        src, dst, args.mode, args.threads, args.dry_run, stats, skip_dirs
    )

    print("-" * 60)
    print(f"mode    : {args.mode}")
    print(f"folders : {stats.folders} sent to robocopy")
    print(f"failed  : {stats.failed}")
    for error in stats.errors[:20]:
        print(f"  ERROR {error}")
    if len(stats.errors) > 20:
        print(f"  ... and {len(stats.errors) - 20} more")
    print(f"done in {time.time() - started:.1f}s")
    return 2 if stats.failed else 0


if __name__ == "__main__":
    sys.exit(main())
