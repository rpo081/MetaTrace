"""Central file-type/size/sequence rules (single source of truth).

All inventory/sync code must import from here. Prevents silent drift where
PSDs are indexed but never mirrored (see architect H-05).
"""
from __future__ import annotations

from pathlib import Path

# Canonical indexed extensions — must match Settings.allowed_extensions default
ALLOWED_EXTENSIONS: frozenset[str] = frozenset({".psd", ".jpg", ".jpeg", ".png", ".tif", ".tiff"})

# Excluded directory basenames (textures/tmp etc) — last 2 path levels checked
EXCLUDED_DIR_NAMES: frozenset[str] = frozenset({"textures", "tmp", ".metatrace_tmp"})
EXCLUDED_DIR_LEVELS: int = 2

MAX_FILE_SIZE_MB: int = 100  # operator tunables: 0 = unlimited
MAX_SEQUENCE_IMAGES: int = 100  # numbered sequences > this are dropped as render bursts


def is_indexable(path: str | Path) -> bool:
    return Path(path).suffix.lower() in ALLOWED_EXTENSIONS


def sequence_key(path: str | Path) -> str | None:
    """Return sequence grouping key for path, or None if not a numbered sequence member."""
    # Mirrors scripts/sync_images.py logic: stem ends with digits => sequence
    stem = Path(path).stem
    # strip trailing digits
    i = len(stem)
    while i > 0 and stem[i - 1].isdigit():
        i -= 1
    if i == len(stem) or i == 0:
        return None
    return stem[:i]
