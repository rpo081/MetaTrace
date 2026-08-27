"""Drive snapshot utilities used by the CLI script and the API."""
from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Collection
from datetime import UTC, datetime
from pathlib import Path


DEFAULT_EXTENSIONS = frozenset({".psd", ".jpg", ".jpeg", ".png", ".tif", ".tiff"})


def scan_drive(
    root_path: Path,
    *,
    allowed_extensions: Collection[str] | None = None,
) -> dict[str, dict[str, float | int]]:
    """Read file mtimes and sizes for indexed image types under root_path."""
    file_state: dict[str, dict[str, float | int]] = {}
    root_str = os.path.abspath(root_path)
    extensions = frozenset(ext.lower() for ext in (allowed_extensions or DEFAULT_EXTENSIONS))
    for root, _, files in os.walk(root_str):
        for name in files:
            if Path(name).suffix.lower() not in extensions:
                continue
            full_path = os.path.join(root, name)
            try:
                stat = os.stat(full_path)
                rel_path = os.path.relpath(full_path, root_str).replace("\\", "/")
                file_state[rel_path] = {"mtime": stat.st_mtime, "size": stat.st_size}
            except (PermissionError, FileNotFoundError):
                continue
    return file_state


def build_snapshot_payload(root_path: Path, file_state: dict[str, dict[str, float | int]]) -> dict:
    return {
        "version": 1,
        "created_utc": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "root_path": os.path.abspath(root_path),
        "file_count": len(file_state),
        "files": file_state,
    }


def load_snapshot_file(path: Path) -> dict[str, dict[str, float | int]]:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict) and "files" in data:
        return data["files"]
    return data


def save_snapshot_files(
    *,
    snapshot_file: Path,
    latest_snapshot_file: Path,
    root_path: Path,
    file_state: dict[str, dict[str, float | int]],
) -> None:
    payload = build_snapshot_payload(root_path, file_state)
    snapshot_file.parent.mkdir(parents=True, exist_ok=True)
    latest_snapshot_file.parent.mkdir(parents=True, exist_ok=True)
    with open(snapshot_file, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    with open(latest_snapshot_file, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)


def generate_rescan_json(
    *,
    data_folder: Path,
    created: set[str],
    deleted: set[str],
    modified: set[str],
) -> Path:
    timestamp = datetime.now().isoformat()
    rescan_data = {
        "timestamp": timestamp,
        "summary": {
            "created_count": len(created),
            "deleted_count": len(deleted),
            "modified_count": len(modified),
            "total_changes": len(created) + len(deleted) + len(modified),
        },
        "changes": {
            "created": sorted(created),
            "deleted": sorted(deleted),
            "modified": sorted(modified),
        },
    }
    data_folder.mkdir(parents=True, exist_ok=True)
    timestamp_str = timestamp.replace(":", "-").split(".")[0]
    output_filename = data_folder / f"rescan_delta_{timestamp_str}.json"
    latest_filename = data_folder / "rescan_delta_latest.json"
    with open(output_filename, "w", encoding="utf-8") as fh:
        json.dump(rescan_data, fh, indent=2)
    with open(latest_filename, "w", encoding="utf-8") as fh:
        json.dump(rescan_data, fh, indent=2)
    return output_filename


def detect_changes(
    *,
    root_path: str | Path,
    snapshot_file: Path,
    data_folder: Path,
    allowed_extensions: Collection[str] | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> dict:
    root = Path(root_path).expanduser()
    if not root.exists():
        raise ValueError(f"scan root does not exist: {root}")
    if not root.is_dir():
        raise ValueError(f"scan root is not a directory: {root}")

    def progress(message: str) -> None:
        if on_progress is not None:
            on_progress(message)

    progress(f"Scanning current state under {root}...")
    start_time = time.time()
    current_state = scan_drive(root, allowed_extensions=allowed_extensions)
    duration_sec = round(time.time() - start_time, 2)
    latest_snapshot_file = data_folder / "store_snapshot_latest.json"
    progress(f"Scan completed in {duration_sec:.2f}s ({len(current_state)} indexed files).")

    if not snapshot_file.exists():
        progress("No previous snapshot found. Saving current state as baseline...")
        save_snapshot_files(
            snapshot_file=snapshot_file,
            latest_snapshot_file=latest_snapshot_file,
            root_path=root,
            file_state=current_state,
        )
        return {
            "root_path": str(root.resolve()),
            "duration_sec": duration_sec,
            "initialized": True,
            "summary": {
                "created_count": 0,
                "deleted_count": 0,
                "modified_count": 0,
                "total_changes": 0,
            },
            "changes": {"created": [], "deleted": [], "modified": []},
            "delta_file": None,
        }

    progress("Loading previous snapshot...")
    previous_state = load_snapshot_file(snapshot_file)
    current_paths = set(current_state.keys())
    previous_paths = set(previous_state.keys())
    created = current_paths - previous_paths
    deleted = previous_paths - current_paths
    common_paths = current_paths & previous_paths
    modified = {path for path in common_paths if current_state[path] != previous_state[path]}
    progress(
        "Computed changes: "
        f"{len(created)} created, {len(deleted)} deleted, {len(modified)} modified."
    )
    delta_file = generate_rescan_json(
        data_folder=data_folder,
        created=created,
        deleted=deleted,
        modified=modified,
    )
    save_snapshot_files(
        snapshot_file=snapshot_file,
        latest_snapshot_file=latest_snapshot_file,
        root_path=root,
        file_state=current_state,
    )
    return {
        "root_path": str(root.resolve()),
        "duration_sec": duration_sec,
        "initialized": False,
        "summary": {
            "created_count": len(created),
            "deleted_count": len(deleted),
            "modified_count": len(modified),
            "total_changes": len(created) + len(deleted) + len(modified),
        },
        "changes": {
            "created": sorted(created),
            "deleted": sorted(deleted),
            "modified": sorted(modified),
        },
        "delta_file": delta_file.name,
    }