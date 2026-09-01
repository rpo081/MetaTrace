"""Checkpoint persistence — extracted from indexer.py.

Handles ``scan_checkpoint.json`` read/write/validation and resume helpers.
Validation guards ``remaining_rel_paths`` as ``list[str]`` contained within the
store (no ``..``, no absolute, no escape via symlink resolve).
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

from .models.scan import ScanReport

log = logging.getLogger(__name__)

SCAN_CHECKPOINT_VERSION = 1


def _model_key(settings) -> str:
    return f"{settings.model_name}:{settings.model_pretrained}"


def _checkpoint_file(settings) -> Path:
    return settings.data_path / "scan_checkpoint.json"


def _is_safe_rel_path(rel_path: str) -> bool:
    normalized = rel_path.replace("\\", "/")
    if normalized.startswith("/") or normalized.startswith("//"):
        return False
    if ".." in normalized.split("/"):
        return False
    if len(normalized) >= 2 and normalized[1] == ":":
        return False
    return True


def _read_resume_checkpoint(settings) -> dict | None:
    path = _checkpoint_file(settings)
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not read scan checkpoint: %s", exc)
        return None
    if data.get("version") != SCAN_CHECKPOINT_VERSION:
        log.warning("ignoring unknown scan checkpoint version: %r", data.get("version"))
        return None
    if data.get("model") != _model_key(settings):
        log.warning("discarding scan checkpoint for different model: %s", data.get("model"))
        _clear_resume_checkpoint(settings)
        return None
    # Validate remaining_rel_paths structurally when phase is pending.
    # Avoid resolving every path during startup: large paused checkpoints on a
    # slow or network-backed store would block the API from becoming ready.
    # Resume-time path materialization still performs containment checks.
    if data.get("phase") == "pending":
        rr = data.get("remaining_rel_paths")
        if rr is None:
            log.warning("scan checkpoint missing remaining_rel_paths (null) — discarding")
            _clear_resume_checkpoint(settings)
            return None
        if not isinstance(rr, list) or not all(isinstance(x, str) for x in rr):
            log.warning("scan checkpoint remaining_rel_paths is not list[str] — discarding")
            _clear_resume_checkpoint(settings)
            return None
        for rel in rr:
            if not _is_safe_rel_path(rel):
                log.warning("scan checkpoint contains unsafe path %r — discarding", rel)
                _clear_resume_checkpoint(settings)
                return None
    return data


def _write_resume_checkpoint(settings, payload: dict) -> None:
    path = _checkpoint_file(settings)
    tmp = path.with_suffix(path.suffix + ".tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def _clear_resume_checkpoint(settings) -> None:
    _checkpoint_file(settings).unlink(missing_ok=True)


def _save_planning_checkpoint(settings, mode: str, force_rebuild: bool, report: ScanReport, delta_info: dict | None = None) -> None:
    payload = {
        "version": SCAN_CHECKPOINT_VERSION,
        "phase": "planning",
        "mode": mode,
        "force_rebuild": force_rebuild,
        "trigger": report.trigger,
        "report": report.as_dict(),
        "delta_info": delta_info,
        "model": _model_key(settings),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _write_resume_checkpoint(settings, payload)


def _save_pending_checkpoint(
    settings,
    mode: str,
    force_rebuild: bool,
    report: ScanReport,
    remaining_rel_paths: list[str],
    remaining_added_rel_paths: set[str],
) -> None:
    payload = {
        "version": SCAN_CHECKPOINT_VERSION,
        "phase": "pending",
        "mode": mode,
        "force_rebuild": force_rebuild,
        "trigger": report.trigger,
        "report": report.as_dict(),
        "remaining_rel_paths": remaining_rel_paths,
        "remaining_added_rel_paths": sorted(
            rel_path for rel_path in remaining_rel_paths if rel_path in remaining_added_rel_paths
        ),
        "model": _model_key(settings),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _write_resume_checkpoint(settings, payload)


def _load_resume_checkpoint(settings, status: dict, pause_gate) -> dict | None:
    """Load and act on a persisted checkpoint at startup.

    Returns the checkpoint dict if a pending resume is available (and mutates
    *status*/*pause_gate* to ``paused``), otherwise clears stale checkpoints
    and returns None.
    """
    checkpoint = _read_resume_checkpoint(settings)
    if checkpoint is None:
        return None
    if checkpoint.get("phase") != "pending":
        # A "planning" checkpoint means the previous scan died before
        # finishing its first batch — resuming would redo everything
        # anyway. Discard it instead of booting into a phantom "paused"
        # state that also blocks RUN_INITIAL_SCAN_ON_START (trigger_now
        # refuses while paused).
        log.info(
            "discarding stale %r-phase scan checkpoint (nothing processed yet)",
            checkpoint.get("phase"),
        )
        _clear_resume_checkpoint(settings)
        return None
    # Pending checkpoint: restore paused state
    pause_gate.clear()
    status["state"] = "paused"
    status["last_report"] = checkpoint.get("report")
    log.info("found paused scan checkpoint with %d remaining file(s)", len(checkpoint.get("remaining_rel_paths", [])))
    return checkpoint


def _report_from_dict(data: dict) -> ScanReport:
    report = ScanReport(trigger=str(data.get("trigger", "resume")))
    report.started_at = float(data.get("started_at", time.time()))
    report.duration_sec = float(data.get("duration_sec", 0.0))
    report.paused_duration_sec = float(data.get("paused_duration_sec", 0.0))
    report.seen = int(data.get("seen", 0))
    report.processed = int(data.get("processed", 0))
    report.added = int(data.get("added", 0))
    report.updated = int(data.get("updated", 0))
    report.removed = int(data.get("removed", 0))
    report.unchanged = int(data.get("unchanged", 0))
    report.failed = int(data.get("failed", 0))
    return report


# ---------------------------------------------------------------------------
# CheckpointManager — stateful facade over the free functions for Indexer
# ---------------------------------------------------------------------------

class CheckpointManager:
    """Stateful manager that mirrors Indexer's checkpoint API.

    The free functions above are the source of truth for I/O/validation;
    this class keeps the in-memory ``_resume_checkpoint`` cache and delegates
    to them so ``Indexer`` can compose rather than inherit.
    """

    def __init__(self, settings) -> None:
        self.settings = settings
        self._resume_checkpoint: dict | None = None

    @property
    def checkpoint_file(self) -> Path:
        return _checkpoint_file(self.settings)

    def read(self) -> dict | None:
        return _read_resume_checkpoint(self.settings)

    def write(self, payload: dict) -> None:
        _write_resume_checkpoint(self.settings, payload)
        self._resume_checkpoint = payload

    def clear(self) -> None:
        self._resume_checkpoint = None
        _clear_resume_checkpoint(self.settings)

    def save_planning(self, mode: str, force_rebuild: bool, report, delta_info=None) -> None:
        _save_planning_checkpoint(self.settings, mode, force_rebuild, report, delta_info)
        # keep cache in sync with what was written
        self._resume_checkpoint = _read_resume_checkpoint(self.settings)

    def save_pending(self, mode: str, force_rebuild: bool, report, remaining_rel_paths, remaining_added_rel_paths) -> None:
        _save_pending_checkpoint(self.settings, mode, force_rebuild, report, remaining_rel_paths, remaining_added_rel_paths)
        self._resume_checkpoint = _read_resume_checkpoint(self.settings)

    def load(self, status: dict, pause_gate) -> dict | None:
        cp = _load_resume_checkpoint(self.settings, status, pause_gate)
        self._resume_checkpoint = cp
        return cp

    def report_from_dict(self, data: dict):
        return _report_from_dict(data)


__all__ = [
    "SCAN_CHECKPOINT_VERSION",
    "_is_safe_rel_path",
    "_read_resume_checkpoint",
    "_write_resume_checkpoint",
    "_clear_resume_checkpoint",
    "_save_planning_checkpoint",
    "_save_pending_checkpoint",
    "_load_resume_checkpoint",
    "_report_from_dict",
    "CheckpointManager",
]
