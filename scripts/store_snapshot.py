import os
import json
import time
from datetime import UTC, datetime
from pathlib import Path

SNAPSHOT_FILE = "ssd_snapshot.json"
DATA_FOLDER = Path(__file__).parent.parent / "data"  # ../data/ Ordner
LATEST_SNAPSHOT_FILE = DATA_FOLDER / "store_snapshot_latest.json"

def generate_rescan_json(created, deleted, modified):
    """Erzeugt ein JSON für die WebApp mit Änderungen als Rescan-Basis."""
    timestamp = datetime.now().isoformat()
    rescan_data = {
        "timestamp": timestamp,
        "summary": {
            "created_count": len(created),
            "deleted_count": len(deleted),
            "modified_count": len(modified),
            "total_changes": len(created) + len(deleted) + len(modified)
        },
        "changes": {
            "created": list(created),
            "deleted": list(deleted),
            "modified": list(modified)
        }
    }
    
    # Sicherstellen, dass data/ Ordner existiert
    DATA_FOLDER.mkdir(parents=True, exist_ok=True)
    
    # Speichern mit Timestamp im Dateinamen
    timestamp_str = timestamp.replace(':', '-').split('.')[0]
    output_filename = DATA_FOLDER / f"rescan_delta_{timestamp_str}.json"
    with open(output_filename, "w") as f:
        json.dump(rescan_data, f, indent=2)
    
    # Immer auch als "latest" speichern für einfachen Zugriff
    latest_filename = DATA_FOLDER / "rescan_delta_latest.json"
    with open(latest_filename, "w") as f:
        json.dump(rescan_data, f, indent=2)
    
    print(f"\n📊 Rescan-Basis erzeugt: {output_filename}")
    print(f"📌 Latest: {latest_filename}")
    return str(output_filename)

def scan_drive(root_path):
    """Liest Pfad, Änderungsdatum (mtime) und Größe extrem schnell ein."""
    file_state = {}
    root_path = os.path.abspath(root_path)
    
    # os.walk mit os.scandir im Hintergrund ist sehr performant
    for root, _, files in os.walk(root_path):
        for name in files:
            full_path = os.path.join(root, name)
            try:
                # os.stat Aufruf vermeiden: scandir liefert Stat-Info oft direkt mit
                stat = os.stat(full_path)
                # Convert to relative path and normalize separators to forward slashes (DB format)
                rel_path = os.path.relpath(full_path, root_path)
                rel_path = rel_path.replace('\\', '/')  # Normalize to forward slashes
                file_state[rel_path] = {"mtime": stat.st_mtime, "size": stat.st_size}
            except (PermissionError, FileNotFoundError):
                continue
    return file_state


def build_snapshot_payload(root_path, file_state):
    return {
        "version": 1,
        "created_utc": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "root_path": os.path.abspath(root_path),
        "file_count": len(file_state),
        "files": file_state,
    }


def load_snapshot_file(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict) and "files" in data:
        return data["files"]
    return data


def save_snapshot_files(root_path, file_state):
    payload = build_snapshot_payload(root_path, file_state)
    with open(SNAPSHOT_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    DATA_FOLDER.mkdir(parents=True, exist_ok=True)
    with open(LATEST_SNAPSHOT_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f)

def detect_changes(root_path):
    print("Scanne aktuellen Zustand der SSD...")
    start_time = time.time()
    current_state = scan_drive(root_path)
    print(f"Scan abgeschlossen in {time.time() - start_time:.2f} Sekunden.")

    if not os.path.exists(SNAPSHOT_FILE):
        print("Kein alter Snapshot gefunden. Speichere aktuellen Zustand als Basis...")
        save_snapshot_files(root_path, current_state)
        return

    print("Lade vorherigen Snapshot...")
    previous_state = load_snapshot_file(SNAPSHOT_FILE)

    # Mengen-Vergleich für extrem schnelle Differenz-Berechnung
    current_paths = set(current_state.keys())
    previous_paths = set(previous_state.keys())

    created = current_paths - previous_paths
    deleted = previous_paths - current_paths
    
    # Prüfen, ob sich mtime oder Größe geändert haben
    common_paths = current_paths & previous_paths
    modified = {
        path for path in common_paths 
        if current_state[path] != previous_state[path]
    }

    # Ergebnisse anzeigen
    print(f"\n--- ERGEBNISSE ({len(created)} neu, {len(deleted)} gelöscht, {len(modified)} geändert) ---")
    
    if created:
        print("\n[+] NEU ERSTELLT:")
        for p in list(created)[:10]: print(f"  {p}")
        if len(created) > 10: print(f"  ... und {len(created)-10} weitere.")

    if deleted:
        print("\n[-] GELÖSCHT:")
        for p in list(deleted)[:10]: print(f"  {p}")
        if len(deleted) > 10: print(f"  ... und {len(deleted)-10} weitere.")

    if modified:
        print("\n[*] GEÄNDERT:")
        for p in list(modified)[:10]: print(f"  {p}")
        if len(modified) > 10: print(f"  ... und {len(modified)-10} weitere.")

    # Rescan-JSON für WebApp erzeugen
    generate_rescan_json(created, deleted, modified)

    # Snapshot aktualisieren
    save_snapshot_files(root_path, current_state)

if __name__ == "__main__":
    # Pfad zur SSD
    DRIVE_TO_SCAN = "G:\\"  
    detect_changes(DRIVE_TO_SCAN)