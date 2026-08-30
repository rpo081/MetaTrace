"""SQLite persistence for image metadata and authentication."""
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
CREATE INDEX IF NOT EXISTS idx_images_mtime ON images(mtime);
CREATE INDEX IF NOT EXISTS idx_images_indexed_at ON images(indexed_at);
CREATE INDEX IF NOT EXISTS idx_images_size ON images(size);
CREATE INDEX IF NOT EXISTS idx_images_rel_path ON images(rel_path);
CREATE INDEX IF NOT EXISTS idx_images_width ON images(width);
CREATE INDEX IF NOT EXISTS idx_images_height ON images(height);

-- Auth tables
CREATE TABLE IF NOT EXISTS users (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    username             TEXT NOT NULL UNIQUE,
    email                TEXT NOT NULL UNIQUE,
    password_hash        TEXT NOT NULL,
    role                 TEXT NOT NULL DEFAULT 'viewer' CHECK(role IN ('admin', 'editor', 'viewer')),
    mfa_secret           TEXT,
    is_active            INTEGER NOT NULL DEFAULT 1,
    must_change_password INTEGER NOT NULL DEFAULT 0,
    created_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    last_login           TEXT,
    failed_attempts      INTEGER NOT NULL DEFAULT 0,
    locked_until         TEXT
);

CREATE TABLE IF NOT EXISTS refresh_tokens (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash       TEXT NOT NULL UNIQUE,
    family_id        TEXT NOT NULL,
    rotation_counter INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    expires_at       TEXT NOT NULL,
    revoked_at       TEXT,
    ip_address       TEXT,
    user_agent       TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER REFERENCES users(id) ON DELETE SET NULL,
    action      TEXT NOT NULL,
    resource    TEXT,
    ip_address  TEXT,
    user_agent  TEXT,
    timestamp   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    details     TEXT
);
"""


@dataclass
class Entry:
    id: int
    rel_path: str
    size: int
    mtime: float


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=60, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # Per-connection pragmas for 200k scale: WAL allows concurrent
    # readers during bulk upserts; NORMAL + busy_timeout avoids
    # SQLITE_BUSY under thumb/browse/scan concurrency.
    try:
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA journal_size_limit=67108864")
        conn.execute("PRAGMA busy_timeout=60000")
        conn.execute("PRAGMA cache_size=-64000")
        conn.execute("PRAGMA temp_store=MEMORY")
    except sqlite3.OperationalError:
        pass
    return conn


def init_db(db_path: Path, *, configure_journal: bool = True) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(connect(db_path)) as conn:
        if configure_journal:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA journal_size_limit=67108864")
        conn.executescript(_SCHEMA)

    # Idempotent column migrations for older deployments whose `users` table
    # pre-dates a given column. CREATE TABLE IF NOT EXISTS leaves the schema
    # untouched, so we apply additive ALTERs here.
    with closing(connect(db_path)) as conn, conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
        if "must_change_password" not in cols:
            conn.execute(
                "ALTER TABLE users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0"
            )


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


_VALID_SORT_COLUMNS = frozenset({
    "indexed_at", "mtime", "size", "rel_path", "width", "height", "id",
})


def browse_images(
    db_path: Path,
    *,
    offset: int = 0,
    limit: int = 60,
    sort: str = "indexed_at",
    order: str = "desc",
    filters: dict | None = None,
) -> tuple[int, list[sqlite3.Row]]:
    """Browse indexed images with filtering, sorting and pagination.

    Returns ``(total_count, rows)`` where *rows* are the page slice.
    """
    filters = filters or {}
    where_clauses: list[str] = []
    params: list[object] = []

    # --- size ---
    if filters.get("size_min") is not None:
        where_clauses.append("size >= ?")
        params.append(int(filters["size_min"]))
    if filters.get("size_max") is not None:
        where_clauses.append("size <= ?")
        params.append(int(filters["size_max"]))
    # --- width ---
    if filters.get("width_min") is not None:
        where_clauses.append("width >= ?")
        params.append(int(filters["width_min"]))
    if filters.get("width_max") is not None:
        where_clauses.append("width <= ?")
        params.append(int(filters["width_max"]))
    # --- height ---
    if filters.get("height_min") is not None:
        where_clauses.append("height >= ?")
        params.append(int(filters["height_min"]))
    if filters.get("height_max") is not None:
        where_clauses.append("height <= ?")
        params.append(int(filters["height_max"]))
    # --- indexed_at ---
    if filters.get("indexed_from") is not None:
        where_clauses.append("indexed_at >= ?")
        params.append(str(filters["indexed_from"]))
    if filters.get("indexed_to") is not None:
        where_clauses.append("indexed_at <= ?")
        params.append(str(filters["indexed_to"]))
    # --- mtime ---
    if filters.get("mtime_from") is not None:
        where_clauses.append("mtime >= ?")
        params.append(float(filters["mtime_from"]))
    if filters.get("mtime_to") is not None:
        where_clauses.append("mtime <= ?")
        params.append(float(filters["mtime_to"]))
    # --- extension (SUBSTR match on rel_path) ---
    if filters.get("ext") is not None:
        ext = filters["ext"]
        if not ext.startswith("."):
            ext = f".{ext}"
        where_clauses.append("SUBSTR(rel_path, -LENGTH(?)) = ?")
        params.extend([ext.lower(), ext.lower()])
    # --- folder (LIKE prefix match) ---
    if filters.get("folder") is not None:
        escaped_folder = escape_like(filters["folder"])
        where_clauses.append("rel_path LIKE ? ESCAPE '\\'")
        params.append(f"{escaped_folder}%")
    # --- text search (LIKE on rel_path + xmp) ---
    if filters.get("q") is not None and filters["q"].strip():
        terms = [t.strip() for t in filters["q"].strip().split() if t.strip()]
        for term in terms:
            escaped = escape_like(term)
            pattern = f"%{escaped}%"
            where_clauses.append(
                "(rel_path LIKE ? ESCAPE '\\' OR xmp LIKE ? ESCAPE '\\')"
            )
            params.extend([pattern, pattern])
    # --- has_xmp ---
    if filters.get("has_xmp"):
        where_clauses.append("xmp != '{}'")

    where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"

    # Validate sort
    if sort not in _VALID_SORT_COLUMNS:
        sort = "indexed_at"
    order_upper = "DESC" if order.lower() == "desc" else "ASC"

    with closing(connect(db_path)) as conn:
        total = int(
            conn.execute(f"SELECT COUNT(*) AS n FROM images WHERE {where_sql}", tuple(params))
            .fetchone()["n"]
        )
        rows = conn.execute(
            f"SELECT * FROM images WHERE {where_sql} ORDER BY {sort} {order_upper} LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()

    return total, list(rows)


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


# ---------------------------------------------------------------------------
# Auth CRUD
# ---------------------------------------------------------------------------

def user_count(db_path: Path) -> int:
    """Return the total number of users."""
    with closing(connect(db_path)) as conn:
        return int(conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"])


def create_user(
    db_path: Path,
    *,
    username: str,
    email: str,
    password_hash: str,
    role: str = "viewer",
    must_change_password: bool = False,
) -> int:
    """Insert a new user and return its id."""
    with closing(connect(db_path)) as conn, conn:
        conn.execute(
            "INSERT INTO users (username, email, password_hash, role, must_change_password) "
            "VALUES (?, ?, ?, ?, ?)",
            (username, email, password_hash, role, int(bool(must_change_password))),
        )
        row = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
    assert row is not None
    return int(row["id"])


def get_must_change_password(db_path: Path, user_id: int) -> bool | None:
    """Return the must_change_password flag for the user, or None if user not found.

    Returning ``None`` (rather than silently coercing to ``False``) for a
    missing user lets callers distinguish "flag is unset" from "no such user"
    — the previous contract masked bugs and made a deleted-but-cached user_id
    look like a normal no-op.
    """
    with closing(connect(db_path)) as conn:
        row = conn.execute(
            "SELECT must_change_password FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    if row is None:
        return None
    return bool(row["must_change_password"])


def clear_must_change_password(db_path: Path, user_id: int) -> None:
    """Clear the must-change-password flag for the given user."""
    with closing(connect(db_path)) as conn, conn:
        conn.execute(
            "UPDATE users SET must_change_password = 0, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE id = ?",
            (user_id,),
        )


def get_user_by_id(db_path: Path, user_id: int) -> sqlite3.Row | None:
    with closing(connect(db_path)) as conn:
        return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def get_user_by_username(db_path: Path, username: str) -> sqlite3.Row | None:
    with closing(connect(db_path)) as conn:
        return conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()


def get_user_by_email(db_path: Path, email: str) -> sqlite3.Row | None:
    with closing(connect(db_path)) as conn:
        return conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()


def list_users(db_path: Path) -> list[sqlite3.Row]:
    with closing(connect(db_path)) as conn:
        return conn.execute("SELECT * FROM users ORDER BY id").fetchall()


def update_user(
    db_path: Path,
    user_id: int,
    *,
    email: str | None = None,
    role: str | None = None,
    is_active: int | None = None,
    password_hash: str | None = None,
) -> bool:
    """Update user fields. Returns True if a row was affected."""
    sets: list[str] = []
    params: list[object] = []
    if email is not None:
        sets.append("email = ?")
        params.append(email)
    if role is not None:
        sets.append("role = ?")
        params.append(role)
    if is_active is not None:
        sets.append("is_active = ?")
        params.append(is_active)
    if password_hash is not None:
        sets.append("password_hash = ?")
        params.append(password_hash)
    if not sets:
        return False
    sets.append("updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')")
    params.append(user_id)
    sql = f"UPDATE users SET {', '.join(sets)} WHERE id = ?"
    with closing(connect(db_path)) as conn, conn:
        cursor = conn.execute(sql, tuple(params))
    return cursor.rowcount > 0


def record_login_success(db_path: Path, user_id: int) -> None:
    """Reset failed_attempts and set last_login."""
    with closing(connect(db_path)) as conn, conn:
        conn.execute(
            """
            UPDATE users
            SET last_login = strftime('%Y-%m-%dT%H:%M:%fZ','now'),
                failed_attempts = 0,
                locked_until = NULL,
                updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
            WHERE id = ?
            """,
            (user_id,),
        )


def record_login_failure(db_path: Path, user_id: int, max_attempts: int, lockout_minutes: int) -> None:
    """Increment failed_attempts and optionally lock the account."""
    with closing(connect(db_path)) as conn, conn:
        conn.execute(
            """
            UPDATE users
            SET failed_attempts = failed_attempts + 1,
                locked_until = CASE
                    WHEN failed_attempts + 1 >= ? THEN
                        datetime('now', '+' || ? || ' minutes')
                    ELSE locked_until
                END,
                updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
            WHERE id = ?
            """,
            (max_attempts, lockout_minutes, user_id),
        )


def delete_user(db_path: Path, user_id: int) -> bool:
    """Delete a user. Returns True if a row was deleted."""
    with closing(connect(db_path)) as conn, conn:
        cursor = conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    return cursor.rowcount > 0


# ── Refresh tokens ──────────────────────────────────────────────────────

def store_refresh_token(
    db_path: Path,
    *,
    user_id: int,
    token_hash: str,
    family_id: str,
    rotation_counter: int = 0,
    expires_at: str,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> int:
    """Persist a refresh token row and return its id."""
    with closing(connect(db_path)) as conn, conn:
        conn.execute(
            """
            INSERT INTO refresh_tokens
                (user_id, token_hash, family_id, rotation_counter, expires_at, ip_address, user_agent)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, token_hash, family_id, rotation_counter, expires_at, ip_address, user_agent),
        )
        row = conn.execute(
            "SELECT id FROM refresh_tokens WHERE token_hash = ?", (token_hash,)
        ).fetchone()
    assert row is not None
    return int(row["id"])


def get_refresh_token_by_hash(db_path: Path, token_hash: str) -> sqlite3.Row | None:
    with closing(connect(db_path)) as conn:
        return conn.execute(
            "SELECT * FROM refresh_tokens WHERE token_hash = ?", (token_hash,)
        ).fetchone()


def revoke_refresh_token(db_path: Path, token_hash: str) -> None:
    """Mark a single refresh token as revoked."""
    with closing(connect(db_path)) as conn, conn:
        conn.execute(
            """
            UPDATE refresh_tokens
            SET revoked_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
            WHERE token_hash = ? AND revoked_at IS NULL
            """,
            (token_hash,),
        )


def revoke_all_user_tokens(db_path: Path, user_id: int) -> None:
    """Revoke every active refresh token for a user."""
    with closing(connect(db_path)) as conn, conn:
        conn.execute(
            """
            UPDATE refresh_tokens
            SET revoked_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
            WHERE user_id = ? AND revoked_at IS NULL
            """,
            (user_id,),
        )


def revoke_token_family(db_path: Path, family_id: str) -> None:
    """Revoke all tokens in a given family (reuse detection)."""
    with closing(connect(db_path)) as conn, conn:
        conn.execute(
            """
            UPDATE refresh_tokens
            SET revoked_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
            WHERE family_id = ? AND revoked_at IS NULL
            """,
            (family_id,),
        )


# ── Audit log ───────────────────────────────────────────────────────────

def audit_log_insert(
    db_path: Path,
    *,
    user_id: int | None = None,
    action: str,
    resource: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    details: str | None = None,
) -> None:
    with closing(connect(db_path)) as conn, conn:
        conn.execute(
            """
            INSERT INTO audit_log (user_id, action, resource, ip_address, user_agent, details)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user_id, action, resource, ip_address, user_agent, details),
        )
