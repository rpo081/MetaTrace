"""Central file-type/size/sequence rules (single source of truth).

All inventory/sync code must import from here. Prevents silent drift where
PSDs are indexed but never mirrored (see architect H-05).
"""
from __future__ import annotations

import os
import re
from collections import Counter
from collections.abc import Iterable
from pathlib import Path

# Canonical indexed extensions — must match Settings.allowed_extensions default
ALLOWED_EXTENSIONS: frozenset[str] = frozenset({".psd", ".jpg", ".jpeg", ".png", ".tif", ".tiff"})

# Excluded directory basenames (textures/tmp etc) — last 2 path levels checked
EXCLUDED_DIR_NAMES: frozenset[str] = frozenset({"textures", "tmp", ".metatrace_tmp"})
EXCLUDED_DIR_LEVELS: int = 2

MAX_FILE_SIZE_MB: int = 100  # operator tunables: 0 = unlimited
MAX_SEQUENCE_IMAGES: int = 100  # numbered sequences > this are dropped as render bursts

# A run of digits marks a frame number, whether trailing (`proj_0001`) or
# embedded (`proj_0001_A`). Every run is normalized so `scene_001_take2`
# groups as `scene_#_take#`, and suffixes after the number stay part of the key
# so `proj_#_A` and `proj_#_B` are separate sequences.
_SEQUENCE_NUMBER_RE = re.compile(r"\d+")


def is_indexable(path: str | Path) -> bool:
    return Path(path).suffix.lower() in ALLOWED_EXTENSIONS


def sequence_key(path: str | Path) -> tuple[str, str, str] | None:
    """Return a grouping key for numbered sequence frames, or None.

    The key is ``(directory, normalized stem, extension)`` so frames only group
    within one folder and one file type. Unnumbered names and names made up of
    only digits/separators return None.
    """
    text = os.fspath(path)
    stem = Path(text).stem
    if not _SEQUENCE_NUMBER_RE.search(stem):
        return None
    normalized = _SEQUENCE_NUMBER_RE.sub("#", stem)
    if not any(ch.isalpha() for ch in normalized):
        return None
    return os.path.dirname(text).casefold(), normalized.casefold(), Path(text).suffix.lower()


def filter_long_sequences(
    rel_paths: Iterable[str],
    max_images: int = MAX_SEQUENCE_IMAGES,
) -> set[str]:
    """Return paths that belong to sequences longer than ``max_images``.

    Sequences are grouped by :func:`sequence_key`; a group above the limit is
    dropped as a render burst, mirroring the network-copy filter.
    """
    keys: dict[str, tuple[str, str, str]] = {}
    for path in rel_paths:
        key = sequence_key(path)
        if key is not None:
            keys[path] = key
    counts = Counter(keys.values())
    return {path for path, key in keys.items() if counts[key] > max_images}
