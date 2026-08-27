"""Runtime configuration, overridable via environment variables or a .env file."""
from __future__ import annotations

from pathlib import Path

from pydantic import field_validator
from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

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

    # NOTE: OpenAI CLIP weights require the -quickgelu architecture variants.
    model_name: str = "ViT-B-32-quickgelu"
    model_pretrained: str = "openai"
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

    allowed_extensions: str = ".psd,.jpg,.jpeg,.png,.tif,.tiff"

    # Shared secret for mutating endpoints (POST /api/rescan). When unset the
    # API runs in trusted-LAN mode: mutations are allowed but a warning is
    # logged once at startup.
    admin_token: str | None = Field(
        default=None,
        validation_alias=AliasChoices("METATRACE_ADMIN_TOKEN", "ADMIN_TOKEN"),
    )

    # Comma-separated list of allowed CORS origins, e.g. "http://localhost:5173".
    # The SPA is served same-origin by default, so NO CORS middleware is
    # attached unless this is set explicitly.
    cors_origins: str | None = Field(
        default=None,
        validation_alias=AliasChoices("METATRACE_CORS_ORIGINS", "CORS_ORIGINS"),
    )

    @field_validator("network_root", "admin_token", "cors_origins", mode="before")
    @classmethod
    def _blank_strings_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
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
