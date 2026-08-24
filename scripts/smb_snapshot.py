import argparse
import json
import os
import subprocess
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, FIRST_COMPLETED, wait

SHARE_ROOT = r"\\erlr165a\visu$\vis_old\vis\_CT_Manual"
WORKERS_SCAN = 16
IS_WINDOWS = os.name == "nt"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SNAPSHOT_FILE = os.path.join(BASE_DIR, "snapshots", "base.json")
DIFF_DIR = os.path.join(BASE_DIR, "snapshots", "diffs")
EXTENSIONS = {".jpg", ".jpeg", ".tif", ".tiff", ".png"}
ROBOCOPY_THREADS = 32
ROBOCOPY_PROCESSES = 8


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


def build_snapshot(roots, workers=WORKERS_SCAN):
    snap = {}
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
                        pending.add(full)
                    else:
                        snap[full] = meta
    return snap


def diff_snapshots(base, new):
    changed = {p: m for p, m in new.items() if base.get(p) != m}
    deleted = sorted(set(base) - set(new))
    return changed, deleted


def load_snapshot(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if data.get("root") != SHARE_ROOT:
        raise SystemExit(f"Snapshot stammt von anderem Share: {data.get('root')}")
    return data["files"]


def _write_json(data, path):
    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def save_snapshot(snap, path):
    data = {
        "version": 1,
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "root": SHARE_ROOT,
        "file_count": len(snap),
        "files": snap,
    }
    _write_json(data, path)


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

    def copy_directory(source_directory, filenames):
        relative_directory = os.path.relpath(source_directory, source_root)
        target_directory = os.path.join(target_root, relative_directory)
        command = [
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
        result = subprocess.run(
            command,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode >= 8:
            raise RuntimeError(
                f"robocopy fehlgeschlagen für {source_directory} "
                f"(Exit-Code {result.returncode})"
            )

    file_count = sum(len(filenames) for filenames in paths_by_directory.values())
    print(f"Kopiere {file_count} geänderte Dateien nach {target_root} ...")
    with ThreadPoolExecutor(
        max_workers=min(ROBOCOPY_PROCESSES, len(paths_by_directory))
    ) as executor:
        futures = [
            executor.submit(copy_directory, source_directory, filenames)
            for source_directory, filenames in paths_by_directory.items()
        ]
        for future in futures:
            future.result()
    return file_count


def save_diff(changed, deleted):
    def matches(path):
        if not EXTENSIONS:
            return True
        return os.path.splitext(os.path.basename(path))[1].lower() in EXTENSIONS

    matched = sorted(p for p in changed if matches(p))
    data = {
        "version": 1,
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "root": SHARE_ROOT,
        "base_snapshot": os.path.abspath(SNAPSHOT_FILE),
        "counts": {
            "new_or_changed": len(changed),
            "deleted": len(deleted),
            "matches": len(matched),
        },
        "new_or_changed": dict(sorted(changed.items())),
        "deleted": sorted(deleted),
        "matches": matched,
    }
    stamp = time.strftime("%Y%m%d_%H%M%S")
    stamped = os.path.join(DIFF_DIR, f"diff_{stamp}.json")
    latest = os.path.join(DIFF_DIR, "latest.json")
    _write_json(data, stamped)
    _write_json(data, latest)
    return stamped


def main(argv=None):
    parser = argparse.ArgumentParser(description="SMB-Share scannen und lokal kopieren")
    parser.add_argument(
        "target",
        help="lokaler Zielpfad für die vollständige Verzeichnisstruktur",
    )
    args = parser.parse_args(argv)

    print(f"Scanne {SHARE_ROOT} ...")
    t0 = time.perf_counter()
    new = build_snapshot([SHARE_ROOT])
    dt = time.perf_counter() - t0
    print(f"{len(new)} Dateien in {dt:.1f}s gefunden")

    if os.path.exists(SNAPSHOT_FILE):
        base = load_snapshot(SNAPSHOT_FILE)
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
        stamped = save_diff(changed, deleted)
        print(f"Diff gespeichert: {stamped}")
        print(f"Diff gespeichert: {os.path.join(DIFF_DIR, 'latest.json')}")
    else:
        print("Erster Lauf: Basis-Snapshot wird erstellt")
        changed = new

    save_snapshot(new, SNAPSHOT_FILE)
    print(f"Snapshot gespeichert: {SNAPSHOT_FILE}")

    copy_paths = sorted(
        path for path in changed
        if os.path.splitext(os.path.basename(path))[1].lower() in EXTENSIONS
    )
    print(f"Es sollen {len(copy_paths)} geänderte Dateien nach {args.target} kopiert werden.")
    confirmation = input("Kopiervorgang starten? [j/N]: ").strip().casefold()
    if confirmation not in {"j", "ja", "y", "yes"}:
        print("Kopiervorgang abgebrochen.")
        return

    try:
        copy_started = time.perf_counter()
        copy_files_robocopy(SHARE_ROOT, args.target, copy_paths)
        copy_duration = time.perf_counter() - copy_started
        print(f"Kopiervorgang abgeschlossen in {copy_duration:.1f} Sekunden.")
    except (OSError, RuntimeError) as exc:
        print(f"FEHLER: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()