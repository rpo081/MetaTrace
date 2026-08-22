"""SQLite persistence for image metadata."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

_SCHEMA = """
CREATE TABLE IF NOT EXISTS images (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    rel_path      TEXT NOT NULL UNIQUE,
    original_path TEXT NOT NULL,
    size          INTEGER NOT NULL,
    mtime         REAL NOT NULL,
    sha256        TEXT,
    width         INTEGER,
    height        INTEGER,
    xmp           TEXT NOT NULL DEFAULT '{}',
    indexed_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@dataclass
class Entry:
    id: int
    rel_path: str
    size: int
    mtime: float


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(connect(db_path)) as conn:
        conn.executescript(_SCHEMA)


def reset(db_path: Path) -> None:
    with closing(connect(db_path)) as conn, conn:
        conn.execute("DELETE FROM images")
        conn.execute("DELETE FROM kv")


def list_entries(db_path: Path) -> dict[str, Entry]:
    """Return {rel_path: Entry} for every indexed image."""
    with closing(connect(db_path)) as conn:
        rows = conn.execute("SELECT id, rel_path, size, mtime FROM images").fetchall()
    return {r["rel_path"]: Entry(r["id"], r["rel_path"], r["size"], r["mtime"]) for r in rows}


def upsert_image(
    db_path: Path,
    *,
    rel_path: str,
    original_path: str,
    size: int,
    mtime: float,
    sha256: str | None,
    width: int | None,
    height: int | None,
    xmp: dict,
) -> int:
    """Insert or update an image row and return its id."""
    with closing(connect(db_path)) as conn, conn:
        conn.execute(
            """
            INSERT INTO images (rel_path, original_path, size, mtime, sha256, width, height, xmp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(rel_path) DO UPDATE SET
                original_path=excluded.original_path,
                size=excluded.size,
                mtime=excluded.mtime,
                sha256=excluded.sha256,
                width=excluded.width,
                height=excluded.height,
                xmp=excluded.xmp,
                indexed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
            """,
            (rel_path, original_path, size, mtime, sha256, width, height,
             json.dumps(xmp, ensure_ascii=False)),
        )
        row = conn.execute("SELECT id FROM images WHERE rel_path = ?", (rel_path,)).fetchone()
    assert row is not None
    return int(row["id"])


def remove_ids(db_path: Path, ids: Sequence[int]) -> None:
    if not ids:
        return
    placeholders = ",".join("?" * len(ids))
    with closing(connect(db_path)) as conn, conn:
        conn.execute(f"DELETE FROM images WHERE id IN ({placeholders})", tuple(ids))


def get_by_id(db_path: Path, image_id: int) -> sqlite3.Row | None:
    with closing(connect(db_path)) as conn:
        return conn.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()


def fetch_by_ids(db_path: Path, ids: Sequence[int]) -> list[sqlite3.Row]:
    """Fetch rows for ids, preserving the order of the input list."""
    rows_by_id = {int(r["id"]): r for r in _fetch_all(db_path, ids)}
    return [rows_by_id[i] for i in ids if i in rows_by_id]


def _fetch_all(db_path: Path, ids: Sequence[int]) -> list[sqlite3.Row]:
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    with closing(connect(db_path)) as conn:
        return conn.execute(
            f"SELECT * FROM images WHERE id IN ({placeholders})", tuple(int(i) for i in ids)
        ).fetchall()


def count(db_path: Path) -> int:
    with closing(connect(db_path)) as conn:
        return int(conn.execute("SELECT COUNT(*) AS n FROM images").fetchone()["n"])


def kv_get(db_path: Path, key: str) -> str | None:
    with closing(connect(db_path)) as conn:
        row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def kv_set(db_path: Path, key: str, value: str) -> None:
    with closing(connect(db_path)) as conn, conn:
        conn.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def row_to_result(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "rel_path": row["rel_path"],
        "original_path": row["original_path"],
        "width": row["width"],
        "height": row["height"],
        "xmp": json.loads(row["xmp"]),
    }
