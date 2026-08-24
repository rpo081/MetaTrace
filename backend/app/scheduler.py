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
