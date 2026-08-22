"""Regression tests for exiftool batch record matching (Bug 2 from audit).

_run() is monkeypatched so these run without exiftool installed; they exercise
the real extract_xmp -> _match_record path.
"""
from pathlib import Path

import pytest

from backend.app import metadata


@pytest.fixture()
def fake_exiftool(monkeypatch):
    """Patch metadata._run to return canned records per invocation."""
    holder = {"records": []}

    def install(records):
        holder["records"] = records
        monkeypatch.setattr(metadata, "_run", lambda files: list(records))

    # Pretend exiftool exists so extract_xmp doesn't short-circuit.
    monkeypatch.setattr(metadata, "exiftool_available", lambda: True)
    return install


def test_exact_sourcefile_wins_over_earlier_basename_hit(fake_exiftool, tmp_path):
    """Pass 1 scans ALL records for an exact SourceFile before any fallback."""
    a = tmp_path / "a.png"
    # record order deliberately adversarial: wrong-file basename match first
    fake_exiftool([
        {"SourceFile": "/elsewhere/a.png", "Title": "wrong"},
        {"SourceFile": str(a), "Title": "right"},
    ])
    out = metadata.extract_xmp([a])
    assert out[str(a)] == {"Title": "right"}


def test_unique_basename_fallback_maps_correctly(fake_exiftool, tmp_path):
    """Fallback fires only when the basename occurs exactly once in the batch."""
    p = tmp_path / "deep" / "nested" / "unique.png"
    # exiftool rewrote the path prefix; basename still unique within batch
    fake_exiftool([
        {"SourceFile": "/mnt/store/deep/nested/unique.png", "Title": "u"},
        {"SourceFile": "/mnt/store/other.png", "Title": "o"},
    ])
    out = metadata.extract_xmp([p])
    assert out[str(p)] == {"Title": "u"}


def test_duplicate_basenames_never_misattributed(fake_exiftool, tmp_path):
    """Duplicate basenames sharing one batch: exact records only, no guessing."""
    a = tmp_path / "a" / "x.png"
    c = tmp_path / "c" / "x.png"
    fake_exiftool([
        {"SourceFile": str(c), "Title": "c-title"},
        {"SourceFile": str(a), "Title": "a-title"},
    ])
    out = metadata.extract_xmp([a, c])
    assert out[str(a)] == {"Title": "a-title"}
    assert out[str(c)] == {"Title": "c-title"}


def test_duplicate_basenames_with_missing_record_refuses_fallback(fake_exiftool, tmp_path):
    """One of two same-basename files has no record -> no fallback attribution."""
    a = tmp_path / "a" / "x.png"
    c = tmp_path / "c" / "x.png"
    fake_exiftool([
        {"SourceFile": str(c), "Title": "c-title"},
    ])
    out = metadata.extract_xmp([a, c])
    assert out[str(c)] == {"Title": "c-title"}
    assert out[str(a)] == {}  # ambiguous basename -> empty, never c's tags


def test_match_record_rejects_partial_basename_suffix():
    """endswith-style matching must not confuse 'ab.png' with 'b.png'."""
    recs = [{"SourceFile": "/x/ab.png"}, {"SourceFile": "/y/b.png"}]
    chunk = ["ab.png", "b.png"]
    assert metadata._match_record(recs, Path("/z/b.png"), chunk) == {"SourceFile": "/y/b.png"}
    assert metadata._match_record(recs, Path("/z/ab.png"), chunk) == {"SourceFile": "/x/ab.png"}
    assert metadata._match_record(recs, Path("/z/zzz.png"), ["zzz.png"]) is None


def test_per_invocation_timeout_is_capped():
    """exiftool worst case must stay bounded (~120s/file, not ~600s+)."""
    assert metadata._TIMEOUT_SEC <= 120
