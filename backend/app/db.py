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
    return conn


def init_db(db_path: Path, *, configure_journal: bool = True) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(connect(db_path)) as conn:
        if configure_journal:
            conn.execute("PRAGMA journal_mode=DELETE")
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


_ID_FETCH_SLICE = 500  # stay well under SQLite's bound-parameter limit


def upsert_images_bulk(db_path: Path, rows: Sequence[dict]) -> dict[str, int]:
    """Upsert many image rows in one connection/transaction.

    Semantically equivalent to calling ``upsert_image`` per row (same conflict
    update incl. indexed_at refresh), minus the per-file connect/commit/select
    overhead. Returns {rel_path: id} for every input row.
    """
    if not rows:
        return {}
    rel_paths = [r["rel_path"] for r in rows]
    id_by_rel: dict[str, int] = {}
    with closing(connect(db_path)) as conn, conn:
        for r in rows:
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
                (
                    r["rel_path"],
                    r["original_path"],
                    r["size"],
                    r["mtime"],
                    r.get("sha256"),
                    r.get("width"),
                    r.get("height"),
                    json.dumps(r.get("xmp") or {}, ensure_ascii=False),
                ),
            )
        for i in range(0, len(rel_paths), _ID_FETCH_SLICE):
            slice_ = rel_paths[i : i + _ID_FETCH_SLICE]
            placeholders = ",".join("?" * len(slice_))
            fetched = conn.execute(
                f"SELECT id, rel_path FROM images WHERE rel_path IN ({placeholders})",
                tuple(slice_),
            ).fetchall()
            for row in fetched:
                id_by_rel[row["rel_path"]] = int(row["id"])
    missing = [rp for rp in rel_paths if rp not in id_by_rel]
    assert not missing, f"bulk upsert lost ids for {len(missing)} row(s), e.g. {missing[:3]}"
    return id_by_rel


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


def kv_delete(db_path: Path, key: str) -> None:
    with closing(connect(db_path)) as conn, conn:
        conn.execute("DELETE FROM kv WHERE key = ?", (key,))


def row_to_result(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "rel_path": row["rel_path"],
        "original_path": row["original_path"],
        "width": row["width"],
        "height": row["height"],
        "xmp": json.loads(row["xmp"]) if isinstance(row["xmp"], str) else row["xmp"],
    }


def update_original_paths(db_path: Path, old_root: str, new_root: str) -> int:
    """Replace *old_root* prefix with *new_root* in every original_path.

    Returns the number of rows affected.  Only touches rows whose
    original_path starts with ``old_root\\`` and does NOT already start with
    ``new_root\\`` (idempotent).  Trailing path separators on both roots are
    stripped before comparison so ``\\nas\\share`` and ``\\nas\\share\\``
    are treated identically.
    """
    if not old_root or not new_root or old_root == new_root:
        return 0
    old = old_root.rstrip("/\\")
    new = new_root.rstrip("/\\")
    sep = chr(92)  # backslash
    old_with_sep = old + sep
    new_with_sep = new + sep
    with closing(connect(db_path)) as conn, conn:
        cursor = conn.execute(
            """
            UPDATE images
            SET original_path = ? || SUBSTR(original_path, LENGTH(?) + 1)
            WHERE SUBSTR(original_path, 1, LENGTH(?) + 1) = ?
              AND SUBSTR(original_path, 1, LENGTH(?) + 1) != ?
            """,
            (new, old, old, old_with_sep, new, new_with_sep),
        )
    return cursor.rowcount


def escape_like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def matches_text(row: sqlite3.Row | dict, q: str) -> bool:
    if not q or not q.strip():
        return True
    terms = [t.lower() for t in q.strip().split() if t]
    if not terms:
        return True
    row_dict = dict(row) if hasattr(row, "keys") else (row if isinstance(row, dict) else {})
    rel = str(row_dict.get("rel_path") or "").lower()
    orig = str(row_dict.get("original_path") or "").lower()
    xmp_val = row_dict.get("xmp", "{}")
    if isinstance(xmp_val, dict):
        xmp_str = json.dumps(xmp_val).lower()
    elif isinstance(xmp_val, str):
        xmp_str = xmp_val.lower()
    else:
        xmp_str = str(xmp_val).lower()

    for term in terms:
        if term not in rel and term not in orig and term not in xmp_str:
            return False
    return True


def search_by_text(db_path: Path, q: str, limit: int = 100) -> list[sqlite3.Row]:
    if not q or not q.strip():
        return []
    terms = [t.strip() for t in q.strip().split() if t.strip()]
    if not terms:
        return []

    where_clauses = []
    params: list[object] = []
    for term in terms:
        escaped = escape_like(term)
        pattern = f"%{escaped}%"
        where_clauses.append("(rel_path LIKE ? ESCAPE '\\' OR original_path LIKE ? ESCAPE '\\' OR xmp LIKE ? ESCAPE '\\')")
        params.extend([pattern, pattern, pattern])

    where_sql = " AND ".join(where_clauses)
    sql = f"SELECT * FROM images WHERE {where_sql} ORDER BY id DESC LIMIT ?"
    params.append(limit)

    with closing(connect(db_path)) as conn:
        return conn.execute(sql, tuple(params)).fetchall()
