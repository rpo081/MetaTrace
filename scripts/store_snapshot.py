import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.app import store_snapshot
from backend.app.config import Settings


def load_settings() -> Settings:
    return Settings(_env_file=REPO_ROOT / ".env")


def default_root_path(settings: Settings | None = None) -> str | None:
    if override := os.environ.get("STORE_SNAPSHOT_ROOT_PATH"):
        return override
    resolved = settings or load_settings()
    return str(resolved.store_path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create/update a store snapshot and rescan delta.")
    parser.add_argument("root_path", nargs="?", default=default_root_path())
    parser.add_argument(
        "--snapshot-file",
        default=str(REPO_ROOT / "data" / "store_snapshot.json"),
        help="Path to the baseline snapshot JSON file.",
    )
    parser.add_argument(
        "--data-dir",
        default=str(REPO_ROOT / "data"),
        help="Directory for latest snapshot and rescan delta files.",
    )
    return parser.parse_args()

if __name__ == "__main__":
    args = parse_args()
    if not args.root_path:
        raise SystemExit(
            "Provide root_path as an argument, set STORE_SNAPSHOT_ROOT_PATH, "
            "or configure LOCAL_IMAGE_STORE in .env."
        )
    result = store_snapshot.detect_changes(
        root_path=args.root_path,
        snapshot_file=Path(args.snapshot_file),
        data_folder=Path(args.data_dir),
        on_progress=print,
    )
    summary = result["summary"]
    if result["initialized"]:
        print(f"Initialized snapshot for {result['root_path']} in {result['duration_sec']:.2f}s.")
    else:
        print(
            f"Scanned {result['root_path']} in {result['duration_sec']:.2f}s: "
            f"{summary['created_count']} created, {summary['deleted_count']} deleted, "
            f"{summary['modified_count']} modified."
        )