"""Background periodic rescans using a daemon thread timer."""
from __future__ import annotations

import logging
import threading

log = logging.getLogger(__name__)


class ScanScheduler:
    """Runs incremental scans on a fixed interval; never overlaps scans."""

    def __init__(self, indexer, interval_min: int):
        self.indexer = indexer
        self.interval_min = max(1, interval_min)
        self._timer: threading.Timer | None = None
        self._busy = threading.Lock()

    def start(self) -> None:
        self._schedule(min(self.interval_min * 60, 300))

    def stop(self) -> None:
        if self._timer is not None:
            self._timer.cancel()

    def _schedule(self, delay_sec: float) -> None:
        self._timer = threading.Timer(delay_sec, self._run_once)
        self._timer.daemon = True
        self._timer.start()

    def _run_once(self) -> None:
        if self._busy.acquire(blocking=False):
            try:
                self.indexer.incremental(trigger="scheduled")
            except Exception:  # noqa: BLE001
                log.exception("scheduled scan failed")
            finally:
                self._busy.release()
        else:
            log.info("previous scan still running; skipping this cycle")
        self._schedule(self.interval_min * 60)

    def trigger_now(self, rebuild: bool = False) -> bool:
        """Start a scan in a background thread. Returns False if one is running."""
        if not self._busy.acquire(blocking=False):
            return False

        def run() -> None:
            try:
                self.indexer.incremental(trigger="manual-api", force_rebuild=rebuild)
            finally:
                self._busy.release()

        threading.Thread(target=run, daemon=True).start()
        return True
