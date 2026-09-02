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
CREATE INDEX IF NOT EXISTS idx_images_indexed_at_id ON images(indexed_at DESC, id DESC);
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


@dataclass(frozen=True)
class ThumbnailCandidate:
    id: int
    rel_path: str
    indexed_at: str


def _filename_of(path: str | None) -> str:
    if not path:
        return ""
    p = str(path).replace("\\", "/").rstrip("/")
    return p.rsplit("/", 1)[-1] if "/" in p else p


def _dirname_of(path: str | None) -> str:
    if not path:
        return ""
    p = str(path).replace("\\", "/").rstrip("/")
    return p.rsplit("/", 1)[0] if "/" in p else ""


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=60, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.create_function("filename_of", 1, _filename_of, deterministic=True)
    conn.create_function("dirname_of", 1, _dirname_of, deterministic=True)
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


def newest_thumbnail_candidates(
    db_path: Path,
    *,
    limit: int,
    before: tuple[str, int] | None = None,
) -> list[ThumbnailCandidate]:
    """Return a newest-first keyset page for idle thumbnail generation."""
    where = ""
    params: list[object] = []
    if before is not None:
        indexed_at, image_id = before
        where = "WHERE indexed_at < ? OR (indexed_at = ? AND id < ?)"
        params.extend((indexed_at, indexed_at, image_id))
    params.append(max(1, limit))
    with closing(connect(db_path)) as conn:
        rows = conn.execute(
            f"""
            SELECT id, rel_path, indexed_at
            FROM images
            {where}
            ORDER BY indexed_at DESC, id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return [
        ThumbnailCandidate(int(row["id"]), str(row["rel_path"]), str(row["indexed_at"]))
        for row in rows
    ]


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


def get_by_id(db_path: Path, image_id: int):  # returns ImageDTO | None
    from .models.scan import row_to_dto

    with closing(connect(db_path)) as conn:
        row = conn.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()
    return row_to_dto(row) if row is not None else None


def fetch_by_ids(db_path: Path, ids: Sequence[int]):  # -> list[ImageDTO]
    """Fetch rows for ids, preserving the order of the input list."""
    rows = _fetch_all(db_path, ids)
    by_id = {int(r.id): r for r in rows}
    return [by_id[i] for i in ids if i in by_id]


def _fetch_all(db_path: Path, ids: Sequence[int]):  # -> list[ImageDTO]
    from .models.scan import row_to_dto

    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    with closing(connect(db_path)) as conn:
        rows = conn.execute(
            f"SELECT * FROM images WHERE id IN ({placeholders})", tuple(int(i) for i in ids)
        ).fetchall()
    return [row_to_dto(r) for r in rows]


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


def display_path(rel_path: str, stored_path: str, path_prefix: str = "") -> str:
    """Return the user-facing image path without changing the stored source path."""
    prefix = path_prefix.strip().rstrip("/\\")
    if not prefix:
        return stored_path
    return f"{prefix}\\{rel_path.replace('/', '\\')}"


def row_to_result(row, path_prefix: str = "") -> dict:
    """Accept ImageDTO or sqlite3.Row and return API-safe dict."""
    # DTO has attributes, Row has mapping
    def _get(key):
        return row[key] if isinstance(row, dict) or hasattr(row, "__getitem__") and key in row else getattr(row, key, None) if hasattr(row, key) else row[key]  # type: ignore

    # Prefer attribute access for DTO
    try:
        rel = getattr(row, "rel_path")
        orig = getattr(row, "original_path")
        width = getattr(row, "width")
        height = getattr(row, "height")
        xmp_val = getattr(row, "xmp")
        rid = getattr(row, "id")
    except AttributeError:
        rel = row["rel_path"]
        orig = row["original_path"]
        width = row["width"]
        height = row["height"]
        xmp_val = row["xmp"]
        rid = row["id"]
    if isinstance(xmp_val, str):
        try:
            xmp = json.loads(xmp_val)
        except Exception:
            xmp = {}
    else:
        xmp = xmp_val if isinstance(xmp_val, dict) else {}
    return {
        "id": rid,
        "rel_path": rel,
        "original_path": display_path(rel, orig, path_prefix),
        "width": width,
        "height": height,
        "xmp": xmp,
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


def _encode_cursor(indexed_at: str, row_id: int) -> str:
    """Encode a cursor for keyset pagination (indexed_at, id)."""
    import base64

    raw = f"{indexed_at}|{row_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[str, int] | None:
    """Decode a cursor string to (indexed_at, id). Returns None on failure."""
    import base64

    try:
        # Pad base64
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode()).decode()
        indexed_at, id_str = raw.rsplit("|", 1)
        return indexed_at, int(id_str)
    except Exception:
        return None


def browse_images(
    db_path: Path,
    *,
    offset: int = 0,
    limit: int = 60,
    sort: str = "indexed_at",
    order: str = "desc",
    filters: dict | None = None,
    cursor: str | None = None,
) -> tuple[int, list[sqlite3.Row]]:
    """Browse indexed images with filtering, sorting and pagination.

    Supports both legacy OFFSET pagination (``offset``) and cursor/keyset
    pagination (``cursor``). When ``cursor`` is provided and ``sort`` is
    ``indexed_at``, uses ``WHERE (indexed_at, id) < (?, ?)`` with the
    composite index ``idx_images_indexed_at_id`` for O(log n) pages instead
    of O(n) OFFSET scans. Falls back to OFFSET for other sort columns.

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
    # --- folder (match anywhere within directory path, excluding filename) ---
    if filters.get("folder") is not None and str(filters["folder"]).strip():
        folder_terms = [t.strip() for t in str(filters["folder"]).strip().split() if t.strip()]
        for term in folder_terms:
            escaped = escape_like(term)
            pattern = f"%{escaped}%"
            where_clauses.append("dirname_of(rel_path) LIKE ? ESCAPE '\\'")
            params.append(pattern)
    # --- filename (match within filename only, not full path or xmp) ---
    fn = filters.get("filename") if filters.get("filename") is not None else filters.get("q")
    if fn is not None and str(fn).strip():
        fn_terms = [t.strip() for t in str(fn).strip().split() if t.strip()]
        for term in fn_terms:
            escaped = escape_like(term)
            pattern = f"%{escaped}%"
            where_clauses.append("filename_of(rel_path) LIKE ? ESCAPE '\\'")
            params.append(pattern)
    # --- xmp text (match within XMP metadata tags only) ---
    xmp_query = filters.get("xmp") if filters.get("xmp") is not None else (filters.get("xmp_query") or filters.get("xmp_tag"))
    if xmp_query is not None and str(xmp_query).strip():
        xmp_terms = [t.strip() for t in str(xmp_query).strip().split() if t.strip()]
        for term in xmp_terms:
            escaped = escape_like(term)
            pattern = f"%{escaped}%"
            where_clauses.append("xmp LIKE ? ESCAPE '\\'")
            params.append(pattern)
    # --- has_xmp ---
    if filters.get("has_xmp"):
        where_clauses.append("xmp != '{}'")

    # Validate sort early (needed for cursor eligibility)
    if sort not in _VALID_SORT_COLUMNS:
        sort = "indexed_at"
    order_upper = "DESC" if order.lower() == "desc" else "ASC"

    # Snapshot base filter state before cursor clause for total count
    base_clauses = list(where_clauses)
    base_params = list(params)

    decoded = _decode_cursor(cursor) if cursor else None
    use_cursor = decoded is not None and sort == "indexed_at"
    if use_cursor and decoded:
        cursor_at, cursor_id = decoded
        # Tuple comparison uses composite index idx_images_indexed_at_id
        if order.lower() == "desc":
            where_clauses.append("(indexed_at, id) < (?, ?)")
        else:
            where_clauses.append("(indexed_at, id) > (?, ?)")
        params.extend([cursor_at, cursor_id])

    where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"
    base_sql = " AND ".join(base_clauses) if base_clauses else "1=1"

    with closing(connect(db_path)) as conn:
        total = int(
            conn.execute(f"SELECT COUNT(*) AS n FROM images WHERE {base_sql}", tuple(base_params)).fetchone()["n"]
        )
        if use_cursor:
            raw = conn.execute(
                f"SELECT * FROM images WHERE {where_sql} ORDER BY {sort} {order_upper} LIMIT ?",
                (*params, limit),
            ).fetchall()
        else:
            raw = conn.execute(
                f"SELECT * FROM images WHERE {where_sql} ORDER BY {sort} {order_upper} LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()

    from .models.scan import row_to_dto

    rows = [row_to_dto(r) for r in raw]
    return total, rows


def escape_like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def matches_text(row, q: str) -> bool:
    if not q or not q.strip():
        return True
    terms = [t.lower() for t in q.strip().split() if t]
    if not terms:
        return True
    # Support ImageDTO (attributes) and legacy Row/dict
    if hasattr(row, "rel_path") and hasattr(row, "original_path"):
        row_dict = {"rel_path": getattr(row, "rel_path"), "original_path": getattr(row, "original_path"), "xmp": getattr(row, "xmp", "{}")}
    elif hasattr(row, "keys"):
        try:
            row_dict = dict(row)
        except Exception:
            row_dict = {k: row[k] for k in row.keys()}  # type: ignore
    elif isinstance(row, dict):
        row_dict = row
    else:
        row_dict = {}
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


def search_by_text(db_path: Path, q: str, limit: int = 100):  # -> list[ImageDTO]
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

    from .models.scan import row_to_dto

    with closing(connect(db_path)) as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
    return [row_to_dto(r) for r in rows]


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


def clear_login_lockout(db_path: Path, user_id: int) -> None:
    """Clear an expired lockout without recording a fresh login event."""
    with closing(connect(db_path)) as conn, conn:
        conn.execute(
            """
            UPDATE users
            SET failed_attempts = 0,
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


def revoke_refresh_token(db_path: Path, token_hash: str) -> int:
    """Mark a single refresh token as revoked.

    Returns the number of rows actually updated (0 or 1). The UPDATE is
    guarded by ``revoked_at IS NULL`` and SQLite serializes writers, so a 0
    here means the token was already revoked/rotated concurrently — callers
    (e.g. atomic refresh-token rotation) treat that as token reuse.
    """
    with closing(connect(db_path)) as conn, conn:
        cur = conn.execute(
            """
            UPDATE refresh_tokens
            SET revoked_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
            WHERE token_hash = ? AND revoked_at IS NULL
            """,
            (token_hash,),
        )
        return int(cur.rowcount)


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
