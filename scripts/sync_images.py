#!/usr/bin/env python3
"""Copy selected image folders from a Windows share with robocopy.

Only PNG, JPG, JPEG, TIF, and TIFF files smaller than 20 MiB are copied. Python
traverses the source tree; robocopy performs each folder transfer using native
workers. Frames of long numbered sequences (>100 images, also when the number is
embedded such as ``project_0001_A.png``) are left behind as render bursts.

Usage:
    python sync_images.py SRC DST --mode {all,final,manual} [--threads N] [--skip-dir PATH] [--dry-run]

Examples:
    python sync_images.py "\\\\server\\share" "G:\\images" --mode final --threads 8
    python sync_images.py "\\\\server\\share" "G:\\images" --mode manual --threads 8
    python sync_images.py "\\\\server\\share" "G:\\images" --mode all --threads 8
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
# Centralised file-type/size rules (single source of truth: backend/app/file_rules.py)
try:
    from backend.app.file_rules import (
        ALLOWED_EXTENSIONS as _CENTRAL_ALLOWED,
        MAX_FILE_SIZE_MB as _CENTRAL_MAX_MB,
        MAX_SEQUENCE_IMAGES as _CENTRAL_MAX_SEQ,
        EXCLUDED_DIR_NAMES as _CENTRAL_EXCLUDED,  # noqa: F401
        filter_long_sequences as _CENTRAL_FILTER_LONG_SEQUENCES,
        sequence_key as _CENTRAL_SEQUENCE_KEY,
    )

    ALLOWED_EXTS = frozenset(e.lower() for e in _CENTRAL_ALLOWED)
    MAX_COPY_SIZE_BYTES = _CENTRAL_MAX_MB * 1024 * 1024 if _CENTRAL_MAX_MB else 0
    # Re-export for callers that introspect constants
    MAX_FILE_SIZE_MB = _CENTRAL_MAX_MB  # type: ignore[no-redef]
    MAX_SEQUENCE_IMAGES = _CENTRAL_MAX_SEQ  # type: ignore[no-redef]
    EXCLUDED_DIR_NAMES = _CENTRAL_EXCLUDED  # type: ignore[no-redef]
    sequence_key = _CENTRAL_SEQUENCE_KEY  # type: ignore[no-redef]
    filter_long_sequences = _CENTRAL_FILTER_LONG_SEQUENCES  # type: ignore[no-redef]
except ImportError:
    import re

    ALLOWED_EXTS = frozenset({".png", ".jpg", ".jpeg", ".tif", ".tiff"})
    MAX_COPY_SIZE_BYTES = 20 * 1024 * 1024
    MAX_FILE_SIZE_MB = 20  # type: ignore[no-redef]
    MAX_SEQUENCE_IMAGES = 100  # type: ignore[no-redef]
    EXCLUDED_DIR_NAMES = frozenset()  # type: ignore[no-redef]
    _SEQUENCE_NUMBER_RE = re.compile(r"\d+")

    def sequence_key(path: str) -> tuple[str, str, str] | None:  # type: ignore[no-redef]
        """Mirror of backend/app/file_rules.sequence_key (standalone fallback)."""
        stem, suffix = os.path.splitext(os.path.basename(path))
        if not _SEQUENCE_NUMBER_RE.search(stem):
            return None
        normalized = _SEQUENCE_NUMBER_RE.sub("#", stem)
        if not any(ch.isalpha() for ch in normalized):
            return None
        return os.path.dirname(path).casefold(), normalized.casefold(), suffix.lower()

    def filter_long_sequences(  # type: ignore[no-redef]
        paths, max_images: int = MAX_SEQUENCE_IMAGES
    ) -> set[str]:
        """Mirror of backend/app/file_rules.filter_long_sequences."""
        from collections import Counter

        keys = {path: key for path, key in ((p, sequence_key(p)) for p in paths) if key is not None}
        counts = Counter(keys.values())
        return {path for path, key in keys.items() if counts[key] > max_images}

SKIP_DIRS = frozenset({
    "$RECYCLE.BIN",
    "System Volume Information",
    ".Spotlight-V100",
    ".Trashes",
    "__MACOSX",
})

# Keep explicit file lists well below the Windows command-line limit. robocopy
# has no file-list option, so sequence-filtered folders are copied in chunks.
ROBOCOPY_COMMAND_CHAR_BUDGET = 30000

MODE_FOLDER_KEYWORDS = {
    "all": (),
    "final": ("final", "_final"),
    "manual": ("manual",),
}


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
    """Return whether a folder name contains any keyword for the selected mode."""
    keywords = MODE_FOLDER_KEYWORDS.get(mode, (mode,))
    return any(any(keyword in part.casefold() for keyword in keywords) for part in relative_dir.parts)


def source_root_matches_mode(src: Path, mode: str) -> bool:
    """Match the selected mode only against the source root's own folder name."""
    return matches_mode(Path(src.name), mode)


def image_masks(mode: str, path_matches: bool) -> tuple[str, ...]:
    """Return robocopy image masks for the selected folder."""
    if mode == "manual" and not path_matches:
        return (
            "*manual*.png", "*manual*.jpg", "*manual*.jpeg",
            "*manual*.tif", "*manual*.tiff",
        )
    return ("*.png", "*.jpg", "*.jpeg", "*.tif", "*.tiff")


def eligible_image_names(filenames, mode: str, path_matches: bool) -> list[str]:
    """Return sorted image names robocopy would copy with ``image_masks``."""
    names = []
    for name in filenames:
        base = Path(name)
        if base.suffix.casefold() not in ALLOWED_EXTS:
            continue
        if mode == "manual" and not path_matches and "manual" not in base.stem.casefold():
            continue
        names.append(name)
    return sorted(names)


def robocopy_command(
    source_dir,
    destination_dir,
    file_args,
    threads: int,
    dry_run: bool,
) -> list[str]:
    """Build the robocopy command for one folder (masks or explicit file names)."""
    command = [
        "robocopy",
        robocopy_path(source_dir),
        robocopy_path(destination_dir),
        *file_args,
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
    return command


def chunk_file_args(
    file_names,
    base_length: int,
    budget: int = ROBOCOPY_COMMAND_CHAR_BUDGET,
) -> list[list[str]]:
    """Split explicit file names into chunks that fit one robocopy command line."""
    chunks: list[list[str]] = []
    current: list[str] = []
    length = base_length
    for name in file_names:
        addition = len(name) + 1
        if current and length + addition > budget:
            chunks.append(current)
            current = []
            length = base_length
        current.append(name)
        length += addition
    if current:
        chunks.append(current)
    return chunks


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
    source_path_matches = source_root_matches_mode(src, mode)
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
        path_matches = mode == "all" or source_path_matches or matches_mode(relative_dir, mode)
        filename_matches = (
            mode == "manual"
            and any(
                "manual" in Path(name).stem.casefold()
                and Path(name).suffix.casefold() in ALLOWED_EXTS
                for name in filenames
            )
        )
        if mode != "all" and not path_matches and not filename_matches:
            continue
        if not any(Path(name).suffix.casefold() in ALLOWED_EXTS for name in filenames):
            continue

        folder = relative_dir.as_posix()
        destination_dir = destination_base / relative_dir
        dropped_frames = filter_long_sequences(
            name for name in filenames if Path(name).suffix.casefold() in ALLOWED_EXTS
        )
        if dropped_frames:
            kept = [
                name
                for name in eligible_image_names(filenames, mode, path_matches)
                if name not in dropped_frames
            ]
            if not kept:
                continue
            base_length = len(
                " ".join(robocopy_command(source_dir, destination_dir, [], threads, dry_run))
            )
            commands = [
                robocopy_command(source_dir, destination_dir, chunk, threads, dry_run)
                for chunk in chunk_file_args(kept, base_length)
            ]
        else:
            commands = [
                robocopy_command(
                    source_dir,
                    destination_dir,
                    image_masks(mode, path_matches),
                    threads,
                    dry_run,
                )
            ]

        if folder != previous_folder:
            print(f"\rcopying folder: {folder}", end="", flush=True)
            previous_folder = folder

        oserror = False
        exit_code_error: int | None = None
        for command in commands:
            try:
                result = subprocess.run(command, check=False)
            except OSError as exc:
                stats.errors.append(f"robocopy {folder}: {exc}")
                oserror = True
                break
            if result.returncode >= 8:
                exit_code_error = result.returncode

        if oserror:
            continue

        stats.folders += 1
        if exit_code_error is not None:
            stats.failed += 1
            stats.errors.append(f"robocopy {folder}: exit code {exit_code_error}")

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
        choices=("all", "final", "manual"),
        required=True,
        help="copy all images or match images below folders containing this value",
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
