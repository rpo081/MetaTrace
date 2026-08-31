import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, FIRST_COMPLETED, wait

SHARE_ROOT = r"\\erlr165a\visu$\vis_old\vis\_CT_Manual"
WORKERS_SCAN = 16
IS_WINDOWS = os.name == "nt"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SNAPSHOT_DIR = os.path.join(BASE_DIR, "snapshots")
DIFF_DIR = os.path.join(SNAPSHOT_DIR, "diffs")
# Centralised rules — single source: backend/app/file_rules.py
try:
    from backend.app.file_rules import (
        ALLOWED_EXTENSIONS as _CENTRAL_ALLOWED,
        EXCLUDED_DIR_LEVELS as _CENTRAL_EXCLUDED_LEVELS,
        EXCLUDED_DIR_NAMES as _CENTRAL_EXCLUDED,
        MAX_FILE_SIZE_MB as _CENTRAL_MAX_MB,
        MAX_SEQUENCE_IMAGES as _CENTRAL_MAX_SEQ,
    )

    EXTENSIONS = set(_CENTRAL_ALLOWED)
    EXCLUDED_DIR_NAMES = set(_CENTRAL_EXCLUDED)
    MAX_SEQUENCE_IMAGES = _CENTRAL_MAX_SEQ
    MAX_FILE_SIZE_MB = _CENTRAL_MAX_MB
    EXCLUDED_DIR_LEVELS = _CENTRAL_EXCLUDED_LEVELS
    EXCLUDED_SCAN_PATHS = set()
except ImportError:
    EXTENSIONS = {".jpg", ".jpeg", ".tif", ".tiff", ".png"}
    EXCLUDED_SCAN_PATHS = set()
    EXCLUDED_DIR_NAMES = {
        "max", "3dsmax", "input", "textures", "texturen", "tex",
        "references", "referenzen",
    }
    MAX_SEQUENCE_IMAGES = 100
    # Dateien über diesem Limit (in MB) werden nicht kopiert; 0 = unbegrenzt.
    MAX_FILE_SIZE_MB = 20
    EXCLUDED_DIR_LEVELS = 2
ROBOCOPY_THREADS = 32
ROBOCOPY_PROCESSES = 8
PROGRESS_UPDATE_SEC = 1.0
PROCESS_POLL_SEC = 0.2


def longpath(p):
    if not IS_WINDOWS:
        return p
    if p.startswith("\\\\?\\"):
        return p
    if p.startswith("\\\\"):
        return "\\\\?\\UNC\\" + p.lstrip("\\")
    return "\\\\?\\" + p


def scan_dir(path):
    out = []
    try:
        with os.scandir(longpath(path)) as it:
            for e in it:
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                full = os.path.join(path, e.name)
                if e.is_dir(follow_symlinks=False):
                    out.append((full, None))
                else:
                    out.append((full, [st.st_mtime_ns, st.st_size]))
    except OSError as exc:
        print(f"WARN: {path}: {exc}", file=sys.stderr)
        return path, None
    return path, out


def build_snapshot(roots, workers=WORKERS_SCAN, excluded_scan_paths=None):
    snap = {}
    root = next(iter(roots))
    pending = set(roots)
    running = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        while pending or running:
            while pending:
                d = pending.pop()
                running[ex.submit(scan_dir, d)] = d
            done, _ = wait(running, return_when=FIRST_COMPLETED)
            for f in done:
                del running[f]
                path, entries = f.result()
                if entries is None:
                    raise RuntimeError(f"Scan fehlgeschlagen: {path}")
                for full, meta in entries:
                    if meta is None:
                        if not is_excluded_scan_path(full, root, excluded_scan_paths):
                            pending.add(full)
                    else:
                        snap[full] = meta
    return snap


def diff_snapshots(base, new):
    changed = {p: m for p, m in new.items() if base.get(p) != m}
    deleted = sorted(set(base) - set(new))
    return changed, deleted


def normalize_excluded_scan_paths(paths):
    """Return normalized, source-relative exclude paths."""
    normalized = set()
    for path in paths or ():
        value = str(path).strip().replace("\\", "/").strip("/")
        if value and value != ".":
            normalized.add(value.casefold())
    return normalized


def is_excluded_scan_path(path, root, excluded_scan_paths):
    """Return whether a directory lies inside one of the excluded scan roots."""
    normalized = normalize_excluded_scan_paths(excluded_scan_paths)
    if not normalized:
        return False
    relative = os.path.relpath(path, root).replace("\\", "/").strip("/").casefold()
    if not relative or relative == ".":
        return False
    return any(
        relative == excluded or relative.startswith(excluded + "/")
        for excluded in normalized
    )


def snapshot_file_for(root):
    """Return a per-share snapshot path so switching sources keeps separate history."""
    slug = re.sub(r"[^A-Za-z0-9]+", "_", root).strip("_").lower()[:60] or "share"
    digest = hashlib.blake2s(root.casefold().encode("utf-8"), digest_size=4).hexdigest()
    return os.path.join(SNAPSHOT_DIR, f"{slug}_{digest}.json")


def load_snapshot(path, root, excluded_scan_paths):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if data.get("root") != root:
        return None
    if normalize_excluded_scan_paths(data.get("excluded_scan_paths", ())) != normalize_excluded_scan_paths(excluded_scan_paths):
        return None
    return data["files"]


def _write_json(data, path):
    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def save_snapshot(snap, path, root, excluded_scan_paths):
    data = {
        "version": 1,
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "root": root,
        "excluded_scan_paths": sorted(normalize_excluded_scan_paths(excluded_scan_paths)),
        "file_count": len(snap),
        "files": snap,
    }
    _write_json(data, path)


def format_size(num_bytes):
    """Return a compact IEC size string for display."""
    value = float(num_bytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024


def format_duration(seconds):
    """Return a short human-readable duration string."""
    total = max(0, int(round(seconds)))
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def render_copy_progress(completed_jobs, total_jobs, completed_files, total_files, elapsed_sec):
    """Return a single-line progress summary based on completed robocopy jobs."""
    ratio = 1.0 if total_jobs <= 0 else min(max(completed_jobs / total_jobs, 0.0), 1.0)
    bar_width = 20
    filled = int(round(ratio * bar_width))
    bar = "#" * filled + "-" * (bar_width - filled)
    percent = int(round(ratio * 100))
    return (
        f"Fortschritt [{bar}] {percent:3d}% · {completed_jobs}/{total_jobs} Verzeichnis-Jobs abgeschlossen · "
        f"{completed_files}/{total_files} Dateien in fertigen Jobs · "
        f"Laufzeit {format_duration(elapsed_sec)}"
    )


def render_copy_dashboard(progress_line, pending_jobs, total_jobs, elapsed_sec, target_root=None):
    """Return a small stable dashboard for interactive copy progress."""
    rows = [
        "Kopiervorgang läuft ...",
        progress_line,
        f"Robocopy-Jobs aktiv: {total_jobs - pending_jobs}/{total_jobs} abgeschlossen · Laufzeit {format_duration(elapsed_sec)}",
    ]
    if target_root:
        rows.insert(1, f"Ziel: {target_root}")
    return rows


def build_copy_preview_rows(
    source_root,
    target_root,
    scan_duration,
    scanned_files,
    changed_files,
    deleted_files,
    image_files,
    filtered_count,
    oversized_count,
    existing_count,
    copy_count,
    copy_bytes,
):
    """Return a cleanup-report-style summary shown before copy confirmation."""
    return [
        "",
        "MetaTrace Network Copy Preview",
        "=" * 60,
        f"Source:      {source_root}",
        f"Target:      {target_root}",
        f"Scanned:     {scanned_files:,} files in {scan_duration:.1f}s",
        "",
        "CHANGE SUMMARY",
        "-" * 60,
        f"New/changed: {changed_files:,}",
        f"Deleted:     {deleted_files:,}",
        f"Image files: {image_files:,}",
        "",
        "COPY FILTERS",
        "-" * 60,
        f"Folder/sequence excluded: {filtered_count:,}",
        f"Oversized excluded:       {oversized_count:,}",
        f"Already at target:        {existing_count:,}",
        "",
        "COPY PLAN",
        "-" * 60,
        f"To copy:     {copy_count:,} files ({format_size(copy_bytes)})",
        "",
    ]


def build_robocopy_command(source_directory, target_directory, filenames):
    """Return the robocopy command used for one directory batch."""
    return [
        "robocopy",
        robocopy_path(source_directory),
        robocopy_path(target_directory),
        *sorted(filenames),
        f"/MT:{ROBOCOPY_THREADS}",
        "/COPY:DAT",
        "/DCOPY:DA",
        "/FFT",
        "/J",
        "/XJ",
        "/R:1",
        "/W:1",
        "/NP",
        "/NFL",
        "/NDL",
        "/NJH",
        "/NJS",
    ]


def write_status_line(line, previous_width=0, stream=None):
    """Rewrite one terminal status line in place and return its visible width."""
    out = sys.stdout if stream is None else stream
    padding = max(0, previous_width - len(line))
    out.write("\r" + line + (" " * padding))
    out.flush()
    return len(line)


def clear_status_line(previous_width, stream=None):
    """Clear a previously written terminal status line."""
    if previous_width <= 0:
        return
    out = sys.stdout if stream is None else stream
    out.write("\r" + (" " * previous_width) + "\r")
    out.flush()


def draw_progress_frame(lines, stream=None):
    """Redraw a compact full-screen progress frame for TTY terminals."""
    out = sys.stdout if stream is None else stream
    out.write("\x1b[H\x1b[2J\x1b[H")
    for line in lines:
        out.write(line + "\n")
    out.flush()


def below_excluded_dir(path, root):
    """Return whether one of the lowest folder levels is excluded by name."""
    relative_dir = os.path.dirname(os.path.relpath(path, root))
    if not relative_dir or relative_dir == ".":
        return False
    return any(
        part.casefold() in EXCLUDED_DIR_NAMES
        for part in relative_dir.split(os.sep)[-EXCLUDED_DIR_LEVELS:]
    )


def sequence_key(path):
    """Return a key grouping frame files of one sequence, or None if unnumbered."""
    stem, extension = os.path.splitext(os.path.basename(path))
    prefix = stem.rstrip("0123456789")
    if prefix == stem:
        return None
    return os.path.dirname(path).casefold(), prefix.casefold(), extension.lower()


def filter_images(paths, all_paths, root):
    """Drop images in excluded folders and frames of long animation sequences."""
    sequence_sizes = Counter(
        key for key in map(sequence_key, all_paths) if key is not None
    )
    return [
        path for path in paths
        if not below_excluded_dir(path, root)
        and sequence_sizes.get(sequence_key(path), 0) <= MAX_SEQUENCE_IMAGES
    ]


def filter_oversized(paths, sizes, limit_mb):
    """Split paths into kept files and files above the size limit.

    ``sizes`` maps full path -> byte size (from a snapshot). Files without a
    known size are kept. ``limit_mb <= 0`` disables the filter.
    """
    if not limit_mb or limit_mb <= 0:
        return list(paths), []
    limit = limit_mb * 1024 * 1024
    kept, too_large = [], []
    for path in paths:
        size = sizes.get(path)
        if size is not None and size > limit:
            too_large.append(path)
        else:
            kept.append(path)
    return kept, too_large


def filter_existing_target_files(paths, source_root, target_root, source_snapshot):
    """Drop files that already exist in the target with the same size."""
    if not os.path.isdir(longpath(target_root)):
        return list(paths), 0
    target_snapshot = build_snapshot([target_root])
    target_sizes = {
        os.path.relpath(path, target_root).casefold(): metadata[1]
        for path, metadata in target_snapshot.items()
    }
    filtered = [
        path for path in paths
        if target_sizes.get(os.path.relpath(path, source_root).casefold())
        != source_snapshot[path][1]
    ]
    return filtered, len(paths) - len(filtered)


def robocopy_path(path):
    """Return a normal path because robocopy rejects extended-length paths."""
    value = os.fspath(path)
    if value.startswith("\\\\?\\UNC\\"):
        return "\\\\" + value[8:]
    if value.startswith("\\\\?\\"):
        return value[4:]
    return value


def copy_files_robocopy(source_root, target_root, changed_paths):
    """Copy only changed files while preserving their directory structure."""
    if not IS_WINDOWS:
        raise RuntimeError("robocopy ist nur unter Windows verfügbar")
    paths_by_directory = defaultdict(list)
    for path in changed_paths:
        if os.path.splitext(os.path.basename(path))[1].lower() in EXTENSIONS:
            paths_by_directory[os.path.dirname(path)].append(os.path.basename(path))
    if not paths_by_directory:
        return 0

    def start_copy_process(source_directory, filenames):
        relative_directory = os.path.relpath(source_directory, source_root)
        target_directory = os.path.join(target_root, relative_directory)
        command = build_robocopy_command(source_directory, target_directory, filenames)
        process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return process, source_directory, len(filenames)

    file_count = sum(len(filenames) for filenames in paths_by_directory.values())
    interactive_frame = sys.stdout.isatty()
    if interactive_frame:
        draw_progress_frame(
            render_copy_dashboard(
                render_copy_progress(0, len(paths_by_directory), 0, file_count, 0.001),
                len(paths_by_directory),
                len(paths_by_directory),
                0,
                target_root=target_root,
            )
        )
    else:
        print(f"Kopiere {file_count} geänderte Dateien nach {target_root} ...")
    batches = list(paths_by_directory.items())
    max_processes = min(ROBOCOPY_PROCESSES, len(batches))
    active = []
    started = time.perf_counter()
    last_progress = started
    progress_width = 0
    total_jobs = len(batches)
    completed_jobs = 0
    completed_files = 0
    progress_line = render_copy_progress(0, total_jobs, 0, file_count, 0.001)
    while batches or active:
        while batches and len(active) < max_processes:
            source_directory, filenames = batches.pop()
            active.append(start_copy_process(source_directory, filenames))

        still_running = []
        for process, source_directory, job_file_count in active:
            returncode = process.poll()
            if returncode is None:
                still_running.append((process, source_directory, job_file_count))
                continue
            completed_jobs += 1
            completed_files += job_file_count
            if returncode >= 8:
                raise RuntimeError(
                    f"robocopy fehlgeschlagen für {source_directory} "
                    f"(Exit-Code {returncode})"
                )
        active = still_running

        now = time.perf_counter()
        should_refresh = (now - last_progress) >= PROGRESS_UPDATE_SEC or not active
        if should_refresh:
            progress_line = render_copy_progress(
                completed_jobs,
                total_jobs,
                completed_files,
                file_count,
                max(now - started, 0.001),
            )
            if interactive_frame:
                draw_progress_frame(
                    render_copy_dashboard(
                        progress_line,
                        total_jobs - completed_jobs,
                        total_jobs,
                        now - started,
                        target_root=target_root,
                    )
                )
            else:
                progress_width = write_status_line(
                    progress_line,
                    previous_width=progress_width,
                )
            last_progress = now

        if active:
            time.sleep(PROCESS_POLL_SEC)

    if interactive_frame:
        draw_progress_frame(
            render_copy_dashboard(
                progress_line,
                0,
                total_jobs,
                max(time.perf_counter() - started, 0.001),
                target_root=target_root,
            )
        )
    elif progress_width:
        clear_status_line(progress_width)
        print(progress_line)
    return file_count


def save_diff(changed, deleted, root, snapshot_file):
    def matches(path):
        if not EXTENSIONS:
            return True
        return os.path.splitext(os.path.basename(path))[1].lower() in EXTENSIONS

    matched = sorted(p for p in changed if matches(p))
    data = {
        "version": 1,
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "root": root,
        "base_snapshot": os.path.abspath(snapshot_file),
        "counts": {
            "new_or_changed": len(changed),
            "deleted": len(deleted),
            "matches": len(matched),
        },
        "new_or_changed": dict(sorted(changed.items())),
        "deleted": sorted(deleted),
        "matches": matched,
    }
    latest = os.path.join(DIFF_DIR, "latest.json")
    _write_json(data, latest)
    return latest


def run(source_root, target_root):
    """Scan the share, report the filtered copy set, and copy after confirmation."""
    snapshot_file = snapshot_file_for(source_root)
    excluded_scan_paths = sorted(normalize_excluded_scan_paths(EXCLUDED_SCAN_PATHS))

    print(f"Scanne {source_root} ...")
    t0 = time.perf_counter()
    new = build_snapshot([source_root], excluded_scan_paths=excluded_scan_paths)
    dt = time.perf_counter() - t0
    print(f"{len(new)} Dateien in {dt:.1f}s gefunden")

    if os.path.exists(snapshot_file):
        base = load_snapshot(snapshot_file, source_root, excluded_scan_paths)
        if base is None:
            print("Snapshot-Konfiguration geändert: Basis-Snapshot wird neu aufgebaut")
            changed = new
            deleted = []
        else:
            changed, deleted = diff_snapshots(base, new)
            print(f"{len(changed)} neu/geaendert, {len(deleted)} geloescht")
            for p in deleted:
                print(f"  GELÖSCHT: {p}")
            t1 = time.perf_counter()
            matched = {
                path for path in changed
                if not EXTENSIONS
                or os.path.splitext(os.path.basename(path))[1].lower() in EXTENSIONS
            }
            print(f"{len(matched)} von {len(changed)} geaenderten Dateien entsprechen "
                  f"den konfigurierten Endungen in {time.perf_counter() - t1:.1f}s")
            print(f"Diff gespeichert: {save_diff(changed, deleted, source_root, snapshot_file)}")
    else:
        print("Erster Lauf: Basis-Snapshot wird erstellt")
        changed = new
        deleted = []

    save_snapshot(new, snapshot_file, source_root, excluded_scan_paths)
    print(f"Snapshot gespeichert: {snapshot_file}")

    image_paths = sorted(
        path for path in changed
        if os.path.splitext(os.path.basename(path))[1].lower() in EXTENSIONS
    )
    all_image_paths = [
        path for path in new
        if os.path.splitext(os.path.basename(path))[1].lower() in EXTENSIONS
    ]
    copy_paths = filter_images(image_paths, all_image_paths, source_root)
    filtered_count = len(image_paths) - len(copy_paths)
    file_sizes = {path: meta[1] for path, meta in new.items()}
    copy_paths, oversized = filter_oversized(copy_paths, file_sizes, MAX_FILE_SIZE_MB)
    if oversized:
        print(f"{len(oversized)} Dateien ueber {MAX_FILE_SIZE_MB} MB ausgeschlossen:")
        for path in oversized[:10]:
            print(f"  ZU GROSS: {path} ({format_size(file_sizes[path])})")
        if len(oversized) > 10:
            print(f"  ... und {len(oversized) - 10} weitere")
    copy_paths, existing_count = filter_existing_target_files(
        copy_paths, source_root, target_root, new
    )
    copy_bytes = sum(changed[path][1] for path in copy_paths)
    for row in build_copy_preview_rows(
        source_root,
        target_root,
        dt,
        len(new),
        len(changed),
        len(deleted),
        len(image_paths),
        filtered_count,
        len(oversized),
        existing_count,
        len(copy_paths),
        copy_bytes,
    ):
        print(row)
    confirmation = input("Kopiervorgang starten? [j/N]: ").strip().casefold()
    if confirmation not in {"j", "ja", "y", "yes"}:
        print("Kopiervorgang abgebrochen.")
        return

    try:
        copy_started = time.perf_counter()
        copy_files_robocopy(source_root, target_root, copy_paths)
        copy_duration = time.perf_counter() - copy_started
        print(f"Kopiervorgang abgeschlossen in {copy_duration:.1f} Sekunden.")
    except (OSError, RuntimeError) as exc:
        print(f"FEHLER: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


def main(argv=None):
    parser = argparse.ArgumentParser(description="SMB-Share scannen und lokal kopieren")
    parser.add_argument(
        "target",
        help="lokaler Zielpfad für die vollständige Verzeichnisstruktur",
    )
    parser.add_argument(
        "--source",
        default=SHARE_ROOT,
        help=f"zu scannender Share (Standard: {SHARE_ROOT})",
    )
    args = parser.parse_args(argv)
    run(args.source, args.target)


if __name__ == "__main__":
    main()