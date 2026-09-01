#!/usr/bin/env python3
"""Scan a drive and report files that MetaTrace won't index.

Identifies non-image files, oversized files, excluded directories,
long animation sequences, and system/hidden content. Can optionally
delete excluded files with confirmation.

Usage:
    python cleanup_report.py /Volumes/1TB
    python cleanup_report.py /Volumes/1TB --output report.json
    python cleanup_report.py /Volumes/1TB --delete
    python cleanup_report.py /Volumes/1TB --delete --delete-categories non_image,oversized
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Rules from the MetaTrace codebase — single source of truth: backend/app/file_rules.py
# ---------------------------------------------------------------------------
try:
    from backend.app.file_rules import (
        ALLOWED_EXTENSIONS as _CENTRAL_ALLOWED,
        EXCLUDED_DIR_LEVELS as _CENTRAL_EXCLUDED_LEVELS,
        EXCLUDED_DIR_NAMES as _CENTRAL_EXCLUDED,
        MAX_FILE_SIZE_MB as _CENTRAL_MAX_MB,
        MAX_SEQUENCE_IMAGES as _CENTRAL_MAX_SEQ,
    )

    ALLOWED_EXTENSIONS = _CENTRAL_ALLOWED
    DEFAULT_MAX_SIZE_MB = _CENTRAL_MAX_MB
    MAX_SEQUENCE_IMAGES = _CENTRAL_MAX_SEQ
    EXCLUDED_DIR_NAMES = _CENTRAL_EXCLUDED
    EXCLUDED_DIR_LEVELS = _CENTRAL_EXCLUDED_LEVELS
except ImportError:
    ALLOWED_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".tif", ".tiff"})
    DEFAULT_MAX_SIZE_MB = 20
    MAX_SEQUENCE_IMAGES = 100
    EXCLUDED_DIR_NAMES = frozenset({
        "max", "3dsmax", "input", "textures", "texturen", "tex",
        "references", "referenzen",
    })
    EXCLUDED_DIR_LEVELS = 3

SYSTEM_DIR_NAMES = frozenset({
    "$RECYCLE.BIN",
    "System Volume Information",
    ".Spotlight-V100",
    ".Trashes",
    "__MACOSX",
})

ALL_CATEGORIES = ("non_image", "oversized", "excluded_dir", "long_sequence", "system_hidden")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------
@dataclass
class FileInfo:
    rel_path: str
    abs_path: str
    size: int
    extension: str


@dataclass
class CategoryStats:
    count: int = 0
    total_bytes: int = 0
    files: list[FileInfo] = field(default_factory=list)

    def add(self, f: FileInfo) -> None:
        self.count += 1
        self.total_bytes += f.size
        self.files.append(f)


@dataclass
class Report:
    root: str
    scanned: int = 0
    scanned_bytes: int = 0
    keepable: int = 0
    keepable_bytes: int = 0
    non_image: CategoryStats = field(default_factory=CategoryStats)
    oversized: CategoryStats = field(default_factory=CategoryStats)
    excluded_dir: CategoryStats = field(default_factory=CategoryStats)
    long_sequence: CategoryStats = field(default_factory=CategoryStats)
    system_hidden: CategoryStats = field(default_factory=CategoryStats)
    skipped_dirs: int = 0
    duration_sec: float = 0.0

    @property
    def removable_count(self) -> int:
        return (
            self.non_image.count
            + self.oversized.count
            + self.excluded_dir.count
            + self.long_sequence.count
            + self.system_hidden.count
        )

    @property
    def removable_bytes(self) -> int:
        return (
            self.non_image.total_bytes
            + self.oversized.total_bytes
            + self.excluded_dir.total_bytes
            + self.long_sequence.total_bytes
            + self.system_hidden.total_bytes
        )

    def get_category(self, name: str) -> CategoryStats:
        return {
            "non_image": self.non_image,
            "oversized": self.oversized,
            "excluded_dir": self.excluded_dir,
            "long_sequence": self.long_sequence,
            "system_hidden": self.system_hidden,
        }[name]

    def categories_with_files(self, names: list[str]):
        for name in names:
            cat = self.get_category(name)
            if cat.files:
                yield name, cat


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def format_size(num_bytes: int) -> str:
    value = float(num_bytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < 1024 or unit == "TiB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} PiB"


def sequence_key(path: str) -> tuple[str, str, str] | None:
    """Return a key grouping frame files of one sequence, or None."""
    stem = Path(path).stem
    extension = Path(path).suffix.lower()
    prefix = stem.rstrip("0123456789")
    if prefix == stem:
        return None
    return (os.path.dirname(path).casefold(), prefix.casefold(), extension)


def below_excluded_dir(rel_dir: str, levels: int) -> bool:
    """Return whether one of the lowest folder levels is excluded by name."""
    parts = rel_dir.replace("\\", "/").split("/")
    if len(parts) < levels:
        levels = len(parts)
    return any(part.casefold() in EXCLUDED_DIR_NAMES for part in parts[-levels:])


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------
def scan_drive(root: str, max_size_mb: int) -> Report:
    report = Report(root=os.path.abspath(root))
    max_size_bytes = max_size_mb * 1024 * 1024 if max_size_mb > 0 else 0
    root = os.path.abspath(root)

    sequence_counts: Counter[tuple[str, str, str]] = Counter()
    all_files: list[FileInfo] = []

    print(f"Scanning {root} ...", file=sys.stderr)
    t0 = time.perf_counter()

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames
            if not d.startswith(".")
            and d not in SYSTEM_DIR_NAMES
        )

        rel_dir = os.path.relpath(dirpath, root).replace("\\", "/")
        if rel_dir == ".":
            rel_dir = ""

        for name in filenames:
            full_path = os.path.join(dirpath, name)
            try:
                st = os.stat(full_path)
            except (OSError, PermissionError):
                continue

            rel_path = os.path.join(rel_dir, name).replace("\\", "/") if rel_dir else name
            ext = Path(name).suffix.lower()
            f = FileInfo(rel_path=rel_path, abs_path=full_path, size=st.st_size, extension=ext)

            report.scanned += 1
            report.scanned_bytes += st.st_size
            all_files.append(f)

            if name in SYSTEM_DIR_NAMES or name.startswith("."):
                report.system_hidden.add(f)
                continue

            if ext not in ALLOWED_EXTENSIONS:
                report.non_image.add(f)
                continue

            if rel_dir and below_excluded_dir(rel_dir, EXCLUDED_DIR_LEVELS):
                report.excluded_dir.add(f)
                continue

            if max_size_bytes and st.st_size > max_size_bytes:
                report.oversized.add(f)
                continue

            key = sequence_key(rel_path)
            if key is not None:
                sequence_counts[key] += 1

            report.keepable += 1
            report.keepable_bytes += st.st_size

    long_sequences = {key for key, count in sequence_counts.items() if count > MAX_SEQUENCE_IMAGES}
    if long_sequences:
        for f in all_files:
            key = sequence_key(f.rel_path)
            if key in long_sequences:
                report.keepable -= 1
                report.keepable_bytes -= f.size
                report.long_sequence.add(f)

    report.duration_sec = time.perf_counter() - t0
    print(f"Scan complete in {report.duration_sec:.1f}s", file=sys.stderr)
    return report


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
def print_report(report: Report, verbose: bool) -> None:
    pct = (
        (report.removable_bytes / report.scanned_bytes * 100)
        if report.scanned_bytes
        else 0
    )

    print()
    print("MetaTrace Drive Cleanup Report")
    print("=" * 60)
    print(f"Source:      {report.root}")
    print(f"Scanned:     {report.scanned:,} files ({format_size(report.scanned_bytes)})")
    print()

    print("EXCLUSION SUMMARY")
    print("-" * 60)
    categories = [
        ("Non-image files", report.non_image),
        ("Oversized", report.oversized),
        ("Excluded directories", report.excluded_dir),
        ("Long sequences (>100 frames)", report.long_sequence),
        ("System/hidden", report.system_hidden),
    ]
    for label, cat in categories:
        if cat.count > 0:
            print(f"  {label:.<40} {cat.count:>8,} files  ({format_size(cat.total_bytes):>10})")

    print(f"  {'TOTAL REMOVABLE':.<40} {report.removable_count:>8,} files  ({format_size(report.removable_bytes):>10}) [{pct:.1f}%]")
    print()
    print(f"  {'FILES TO KEEP (indexable)':.<40} {report.keepable:>8,} files  ({format_size(report.keepable_bytes):>10})")
    print()

    print("DETAILED BREAKDOWN")
    print("-" * 60)

    if report.non_image.count:
        ext_stats: dict[str, tuple[int, int]] = defaultdict(lambda: [0, 0])
        for f in report.non_image.files:
            ext_stats[f.extension][0] += 1
            ext_stats[f.extension][1] += f.size
        sorted_exts = sorted(ext_stats.items(), key=lambda x: x[1][1], reverse=True)
        print(f"\n[1] Non-image files by extension:")
        for ext, (count, size) in sorted_exts[:15]:
            print(f"    {ext or '(none)':<10} {count:>6,} files  {format_size(size):>10}")
        if len(sorted_exts) > 15:
            remaining_count = sum(c for _, (c, _) in sorted_exts[15:])
            remaining_size = sum(s for _, (_, s) in sorted_exts[15:])
            print(f"    {'other':<10} {remaining_count:>6,} files  {format_size(remaining_size):>10}")

    if report.oversized.count:
        print(f"\n[2] Oversized files:")
        sorted_oversized = sorted(report.oversized.files, key=lambda f: f.size, reverse=True)
        for f in sorted_oversized[:20]:
            print(f"    {format_size(f.size):>10}  {f.rel_path}")
        if report.oversized.count > 20:
            print(f"    ... and {report.oversized.count - 20} more")

    if report.excluded_dir.count:
        print(f"\n[3] Excluded directories:")
        dir_stats: dict[str, tuple[int, int]] = defaultdict(lambda: [0, 0])
        for f in report.excluded_dir.files:
            parts = f.rel_path.split("/")
            excluded_name = "unknown"
            for part in parts[:-1]:
                if part.casefold() in EXCLUDED_DIR_NAMES:
                    excluded_name = part
                    break
            dir_stats[excluded_name][0] += 1
            dir_stats[excluded_name][1] += f.size
        sorted_dirs = sorted(dir_stats.items(), key=lambda x: x[1][1], reverse=True)
        for name, (count, size) in sorted_dirs:
            print(f"    {name + '/':<20} {count:>6,} files  {format_size(size):>10}")

    if report.long_sequence.count:
        print(f"\n[4] Long animation sequences (>{MAX_SEQUENCE_IMAGES} frames):")
        seq_stats: dict[tuple, tuple[int, int, list[str]]] = {}
        for f in report.long_sequence.files:
            key = sequence_key(f.rel_path)
            if key and key not in seq_stats:
                seq_stats[key] = [0, 0, []]
            if key:
                seq_stats[key][0] += 1
                seq_stats[key][1] += f.size
                if len(seq_stats[key][2]) < 2:
                    seq_stats[key][2].append(os.path.dirname(f.rel_path))
        for key, (count, size, dirs) in sorted(seq_stats.items(), key=lambda x: x[1][1], reverse=True):
            dir_display = dirs[0] if dirs else "unknown"
            if len(dirs) > 1:
                dir_display += " ..."
            print(f"    {count:>5} frames  {format_size(size):>10}  {dir_display}")

    if report.system_hidden.count:
        print(f"\n[5] System/hidden:")
        sys_stats: dict[str, tuple[int, int]] = defaultdict(lambda: [0, 0])
        for f in report.system_hidden.files:
            top_dir = f.rel_path.split("/")[0] if "/" in f.rel_path else f.rel_path
            sys_stats[top_dir][0] += 1
            sys_stats[top_dir][1] += f.size
        for name, (count, size) in sorted(sys_stats.items(), key=lambda x: x[1][1], reverse=True):
            print(f"    {name + '/':<30} {count:>6,} files  {format_size(size):>10}")

    print()


def save_json(report: Report, output_path: str) -> None:
    def cat_dict(cat: CategoryStats) -> dict:
        return {
            "count": cat.count,
            "total_bytes": cat.total_bytes,
            "total_human": format_size(cat.total_bytes),
            "files": [
                {"path": f.rel_path, "size": f.size, "size_human": format_size(f.size)}
                for f in cat.files[:500]
            ],
        }

    data = {
        "root": report.root,
        "scanned_count": report.scanned,
        "scanned_bytes": report.scanned_bytes,
        "scanned_human": format_size(report.scanned_bytes),
        "keepable_count": report.keepable,
        "keepable_bytes": report.keepable_bytes,
        "removable_count": report.removable_count,
        "removable_bytes": report.removable_bytes,
        "removable_pct": round(report.removable_bytes / report.scanned_bytes * 100, 1) if report.scanned_bytes else 0,
        "duration_sec": round(report.duration_sec, 2),
        "categories": {
            "non_image": cat_dict(report.non_image),
            "oversized": cat_dict(report.oversized),
            "excluded_dir": cat_dict(report.excluded_dir),
            "long_sequence": cat_dict(report.long_sequence),
            "system_hidden": cat_dict(report.system_hidden),
        },
    }
    with open(output_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    print(f"JSON report saved to {output_path}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Deletion
# ---------------------------------------------------------------------------
def delete_files(
    report: Report,
    categories: list[str],
    log_path: str | None = None,
) -> tuple[int, int]:
    """Delete files from selected categories. Returns (count, bytes)."""
    total_count = 0
    total_bytes = 0
    log_fh = open(log_path, "w", encoding="utf-8") if log_path else None

    try:
        for name, cat in report.categories_with_files(categories):
            print(f"\nDeleting {name} ({cat.count:,} files, {format_size(cat.total_bytes)}) ...")
            deleted = 0
            failed = 0
            for f in cat.files:
                try:
                    os.remove(f.abs_path)
                    deleted += 1
                    total_count += 1
                    total_bytes += f.size
                    if log_fh:
                        log_fh.write(f"DELETED\t{f.abs_path}\t{f.size}\n")
                except OSError as exc:
                    failed += 1
                    print(f"  FAILED: {f.rel_path} ({exc})", file=sys.stderr)
                    if log_fh:
                        log_fh.write(f"FAILED\t{f.abs_path}\t{exc}\n")
            print(f"  Done: {deleted:,} deleted, {failed} failed")
    finally:
        if log_fh:
            log_fh.close()

    return total_count, total_bytes


def prompt_delete(report: Report, categories: list[str]) -> bool:
    """Ask for confirmation before deleting."""
    cat_names = {
        "non_image": "Non-image files",
        "oversized": "Oversized files",
        "excluded_dir": "Excluded directories",
        "long_sequence": "Long animation sequences",
        "system_hidden": "System/hidden",
    }

    total_count = 0
    total_bytes = 0
    print("\n" + "=" * 60)
    print("DELETE CONFIRMATION")
    print("=" * 60)
    for name in categories:
        cat = report.get_category(name)
        if cat.files:
            print(f"  {cat_names[name]:.<40} {cat.count:>8,} files  ({format_size(cat.total_bytes):>10})")
            total_count += cat.count
            total_bytes += cat.total_bytes

    if total_count == 0:
        print("  Nothing to delete.")
        return False

    print(f"  {'TOTAL':.<40} {total_count:>8,} files  ({format_size(total_bytes):>10})")
    print()
    print("  This cannot be undone!")
    print()

    answer = input("  Type YES to confirm deletion: ").strip()
    return answer == "YES"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("root", help="drive or folder to scan")
    parser.add_argument(
        "--output", "-o",
        metavar="PATH",
        help="save JSON report to this file",
    )
    parser.add_argument(
        "--max-size-mb",
        type=int,
        default=DEFAULT_MAX_SIZE_MB,
        help=f"max file size in MiB (default: {DEFAULT_MAX_SIZE_MB}; 0 = no limit)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="list every excluded file",
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help="delete excluded files (asks for confirmation)",
    )
    parser.add_argument(
        "--delete-categories",
        metavar="CATS",
        help=f"comma-separated categories to delete: {','.join(ALL_CATEGORIES)} (default: all with files)",
    )
    parser.add_argument(
        "--delete-log",
        metavar="PATH",
        help="log deleted files to this path",
    )
    args = parser.parse_args(argv)

    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        print(f"Error: not a directory: {root}", file=sys.stderr)
        return 1

    report = scan_drive(root, args.max_size_mb)
    print_report(report, args.verbose)

    if args.output:
        save_json(report, args.output)

    if args.delete:
        if args.delete_categories:
            categories = [c.strip() for c in args.delete_categories.split(",")]
            invalid = [c for c in categories if c not in ALL_CATEGORIES]
            if invalid:
                print(f"Error: unknown categories: {', '.join(invalid)}", file=sys.stderr)
                print(f"Valid categories: {', '.join(ALL_CATEGORIES)}", file=sys.stderr)
                return 1
        else:
            categories = list(ALL_CATEGORIES)

        if not prompt_delete(report, categories):
            print("Aborted.")
            return 0

        count, total = delete_files(report, categories, args.delete_log)
        print(f"\nDeleted {count:,} files ({format_size(total)}).")
        if args.delete_log:
            print(f"Log saved to {args.delete_log}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
