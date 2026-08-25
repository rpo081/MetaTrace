"""Background manual rescans that never overlap."""
from __future__ import annotations

import threading


class ScanScheduler:
    """Runs explicitly requested incremental scans without overlap."""

    def __init__(self, indexer):
        self.indexer = indexer
        self._busy = threading.Lock()

    def stop(self) -> None:
        """Compatibility hook for application shutdown."""

    def pause(self) -> bool:
        """Request that the running scan pauses at the next checkpoint."""
        return self.indexer.request_pause()

    def resume(self) -> bool:
        """Resume a paused scan."""
        if self.indexer.resume():
            return True
        if self.indexer.status.get("state") != "paused" or not self.indexer.has_resume_checkpoint():
            return False
        if not self._busy.acquire(blocking=False):
            return False

        def run() -> None:
            try:
                self.indexer.resume_from_checkpoint()
            finally:
                self._busy.release()

        threading.Thread(target=run, daemon=True).start()
        return True

    def trigger_now(self, rebuild: bool = False, delta_info: dict | None = None) -> bool:
        """Start a scan in a background thread. Returns False if one is running.
        
        Args:
            rebuild: Force complete index rebuild
            delta_info: Optional delta information from store_snapshot.py
        """
        if self.indexer.status.get("state") == "paused":
            return False
        if not self._busy.acquire(blocking=False):
            return False

        def run() -> None:
            try:
                self.indexer.incremental(
                    trigger="manual-api",
                    force_rebuild=rebuild,
                    delta_info=delta_info
                )
            finally:
                self._busy.release()

        threading.Thread(target=run, daemon=True).start()
        return True
