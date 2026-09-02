"""Runtime configuration, overridable via environment variables or a .env file.

``.env`` loading:
    Pydantic-settings reads ``.env`` from the current working directory (CWD) at
    import time. When running via ``uvicorn app.main:app --app-dir backend`` the
    CWD is typically the repo root, so ``.env`` at ``<repo>/.env`` is picked up.
    If you launch the server from a different directory, set ``STORE_PATH``,
    ``METATRACE_JWT_SECRET`` etc. via environment variables instead, or pass
    ``env_file`` explicitly. ``Settings()`` without args will *not* search parent
    directories — it only looks at ``./.env`` relative to the process CWD.
"""
from __future__ import annotations

from pathlib import Path

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .file_rules import ALLOWED_EXTENSIONS


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore",
        populate_by_name=True,
    )

    # Local mirror of the network share (mounted read-only inside the container).
    store_path: Path = Field(
        default=Path("/store"),
        validation_alias=AliasChoices("LOCAL_IMAGE_STORE", "STORE_PATH"),
    )
    # Writable app data: SQLite database, FAISS index, thumbnail cache.
    data_path: Path = Path("/data")
    # Network share root, e.g. \\nas\share\renderings. When set, every result's
    # original_path is derived as <network_root>\<rel_path with backslashes>.
    network_root: str | None = None

    # NOTE: OpenAI CLIP weights require the -quickgelu variants; SigLIP/WebLI
    # uses e.g. ViT-B-16-SigLIP with pretrained="webli" (see .env.example).
    model_name: str = "ViT-B-16-SigLIP"
    model_pretrained: str = "webli"
    # auto = cuda > mps > cpu. Use "cpu" on macOS: faiss + MPS segfault.
    device: str = "auto"
    # Images per embedding batch. 64 keeps peak RAM sane for print-resolution
    # renders (a single decoded frame can be hundreds of MB); GPU hosts can
    # raise it via .env for throughput.
    batch_size: int = 64
    # Scan-time decode parallelism: decode+hash workers and the max number of
    # fully decoded images held in memory at once (bounded window).
    decode_workers: int = 4
    decode_prefetch: int = 16

    run_initial_scan_on_start: bool = True
    preload_model_on_start: bool = Field(
        default=False,
        validation_alias=AliasChoices("METATRACE_PRELOAD_MODEL_ON_START", "PRELOAD_MODEL_ON_START"),
        description="Load the embedding model before the API reports ready.",
    )
    use_store_snapshot_for_initial_scan: bool = True
    snapshot_scan_root: Path | None = Field(
        default=None,
        validation_alias=AliasChoices("METATRACE_SNAPSHOT_SCAN_ROOT", "SNAPSHOT_SCAN_ROOT"),
    )

    default_top_k: int = 24
    max_top_k: int = 200
    min_score_default: float = 0.0
    thumb_size: int = 256
    max_upload_mb: int = 64
    max_browse_limit: int = 200
    thumbs_max_files: int = Field(
        default=100000,
        validation_alias=AliasChoices("METATRACE_THUMBS_MAX_FILES", "THUMBS_MAX_FILES"),
        ge=0,
        description="Max cached thumbnails before LRU eviction (0=unbounded). 100k ≈5GB at 256px.",
    )
    idle_thumbnails_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("METATRACE_IDLE_THUMBNAILS_ENABLED", "IDLE_THUMBNAILS_ENABLED"),
    )
    idle_thumbnail_grace_sec: float = Field(
        default=15.0,
        validation_alias=AliasChoices("METATRACE_IDLE_THUMBNAIL_GRACE_SEC", "IDLE_THUMBNAIL_GRACE_SEC"),
        ge=0,
    )
    idle_thumbnail_delay_ms: int = Field(
        default=300,
        validation_alias=AliasChoices("METATRACE_IDLE_THUMBNAIL_DELAY_MS", "IDLE_THUMBNAIL_DELAY_MS"),
        ge=0,
    )
    idle_thumbnail_query_batch: int = Field(
        default=100,
        validation_alias=AliasChoices("METATRACE_IDLE_THUMBNAIL_QUERY_BATCH", "IDLE_THUMBNAIL_QUERY_BATCH"),
        ge=1,
        le=1000,
    )
    snapshot_max_age_hours: int = Field(
        default=24,
        validation_alias=AliasChoices("METATRACE_SNAPSHOT_MAX_AGE_HOURS", "SNAPSHOT_MAX_AGE_HOURS"),
        ge=0,
        description="Max age in hours before store snapshot is considered stale (0=never stale). Walk fallback.",
    )

    allowed_extensions: str = ",".join(sorted(ALLOWED_EXTENSIONS))

    # Shared secret for mutating endpoints (POST /api/rescan). When unset the
    # API runs in trusted-LAN mode: mutations are allowed but a warning is
    # logged once at startup.
    admin_token: str | None = Field(
        default=None,
        validation_alias=AliasChoices("METATRACE_ADMIN_TOKEN", "ADMIN_TOKEN"),
    )

    # ── JWT / auth settings ──────────────────────────────────────────────
    jwt_secret: str | None = Field(
        default=None,
        validation_alias=AliasChoices("METATRACE_JWT_SECRET", "JWT_SECRET"),
    )
    access_token_ttl_minutes: int = 15
    refresh_token_ttl_days: int = 7
    max_failed_attempts: int = 5
    lockout_duration_minutes: int = 15
    cookie_secure: bool | None = Field(
        default=None,
        validation_alias=AliasChoices("METATRACE_COOKIE_SECURE", "COOKIE_SECURE"),
        description="Cookie Secure flag: None=auto (https→Secure, http→not), true/false explicit override.",
    )
    cookie_same_site: str = "lax"

    @field_validator("cookie_secure", mode="before")
    @classmethod
    def _cookie_secure_coerce(cls, value: object) -> bool | None:
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and not isinstance(value, bool):
            return bool(value)
        if isinstance(value, str):
            s = value.strip().lower()
            if s in ("", "auto", "none", "null"):
                return None
            if s in ("true", "1", "yes", "on"):
                return True
            if s in ("false", "0", "no", "off"):
                return False
            raise ValueError(
                f"invalid METATRACE_COOKIE_SECURE value {value!r}: expected auto, true, or false"
            )
        raise ValueError(
            f"invalid METATRACE_COOKIE_SECURE value {value!r}: expected auto, true, or false"
        )

    @field_validator("jwt_secret", mode="before")
    @classmethod
    def _jwt_secret_blank_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("jwt_secret", mode="after")
    @classmethod
    def _jwt_secret_min_length(cls, value: str | None) -> str | None:
        if value is not None and len(value) < 32:
            raise ValueError(
                "jwt_secret must be at least 32 characters for HS256 security"
            )
        return value

    # When True, unauthenticated access is allowed (trusted-LAN mode).
    # Must be explicitly opted-in; unset admin_token alone no longer grants
    # open access (M-2 security fix).
    allow_unauthenticated: bool = Field(
        default=False,
        validation_alias=AliasChoices("METATRACE_ALLOW_UNAUTH", "ALLOW_UNAUTH"),
    )

    # Comma-separated list of allowed CORS origins, e.g. "http://localhost:5173".
    # The SPA is served same-origin by default, so NO CORS middleware is
    # attached unless this is set explicitly.
    cors_origins: str | None = Field(
        default=None,
        validation_alias=AliasChoices("METATRACE_CORS_ORIGINS", "CORS_ORIGINS"),
    )

    # When True, `X-Forwarded-For` is trusted for rate-limit keys (behind reverse proxy).
    # Otherwise the direct remote address is used to avoid spoofing.
    trusted_proxy: bool = Field(
        default=False,
        validation_alias=AliasChoices("METATRACE_TRUSTED_PROXY", "TRUSTED_PROXY"),
    )

    @field_validator("network_root", "admin_token", "cors_origins", mode="before")
    @classmethod
    def _blank_strings_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("admin_token", mode="after")
    @classmethod
    def _admin_token_min_length(cls, value: str | None) -> str | None:
        if value is not None and len(value) < 32:
            raise ValueError(
                "admin_token must be at least 32 characters (use `python -c \"import secrets; print(secrets.token_urlsafe(32))\"`)"
            )
        return value

    @property
    def cors_origin_list(self) -> list[str]:
        if not self.cors_origins:
            return []
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def extensions(self) -> frozenset[str]:
        return frozenset(
            e.strip().lower() for e in self.allowed_extensions.split(",") if e.strip()
        )

    @property
    def db_path(self) -> Path:
        return self.data_path / "metatrace.db"

    @property
    def index_file(self) -> Path:
        return self.data_path / "index.faiss"

    @property
    def thumbs_dir(self) -> Path:
        return self.data_path / "thumbs"

    @property
    def latest_store_snapshot_file(self) -> Path:
        return self.data_path / "store_snapshot_latest.json"

    @property
    def baseline_snapshot_file(self) -> Path:
        return self.data_path / "store_snapshot.json"

    @property
    def default_snapshot_scan_root(self) -> Path:
        return self.snapshot_scan_root or self.store_path

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    def ensure_dirs(self) -> None:
        self.data_path.mkdir(parents=True, exist_ok=True)
        self.thumbs_dir.mkdir(parents=True, exist_ok=True)

    def original_path_for(self, rel_path: str) -> str:
        """Map a store-relative path to its original location on the network share."""
        if not self.network_root:
            return rel_path
        root = self.network_root.rstrip("/\\")
        sep = chr(92)  # backslash (not allowed literally inside f-string exprs on 3.11)
        return f"{root}{sep}{rel_path.replace('/', sep)}"
