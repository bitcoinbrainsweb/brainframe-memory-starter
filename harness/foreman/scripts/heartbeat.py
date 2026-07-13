"""Foreman heartbeat: background thread writing last_heartbeat_at.

Starts on task claim(), stops on terminal transition.
Stale-claim recovery uses heartbeat threshold: FOREMAN_STALE_MISS_COUNT * FOREMAN_HEARTBEAT_INTERVAL_S.
Pre-Phase-3 rows (NULL last_heartbeat_at) fall back to a 30-minute fixed threshold.
"""
from __future__ import annotations

import os
import sys
import threading
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .ledger import LedgerBackend


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ForemanHeartbeat:
    """Background heartbeat thread for a single in-flight task.

    Usage:
        hb = ForemanHeartbeat()
        hb.start(run_id, spec_slug, ledger)
        # ... task work runs ...
        hb.stop()   # call on any terminal transition; last_heartbeat_at is retained
    """

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock() # guards all last_heartbeat_at writes
        self._interval_s = int(os.environ.get("FOREMAN_HEARTBEAT_INTERVAL_S", "60"))

    def start(self, run_id: str, spec_slug: str, ledger: "LedgerBackend") -> None:
        """Start the background heartbeat thread. Safe to call only once per task."""
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop,
            args=(run_id, spec_slug, ledger),
            daemon=True,
            name=f"foreman-hb-{spec_slug}",
        )
        self._thread.start()

    def stop(self) -> None:
        """Signal stop and join. last_heartbeat_at retains its last written value."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=max(self._interval_s + 5, 10))
            self._thread = None

    def _loop(self, run_id: str, spec_slug: str, ledger: "LedgerBackend") -> None:
        while not self._stop_event.wait(timeout=self._interval_s):
            self._write(run_id, spec_slug, ledger)

    def _write(self, run_id: str, spec_slug: str, ledger: "LedgerBackend") -> None:
        try:
            with self._lock:
                ledger.patch_heartbeat(run_id, spec_slug, _now_iso())
        except Exception as exc:
            # write failures log to stderr and retry next interval; never raise
            print(f"[heartbeat] write failed for {spec_slug!r}: {exc}", file=sys.stderr)


def heartbeat_stale_threshold_s() -> int:
    """Stale threshold = FOREMAN_STALE_MISS_COUNT * FOREMAN_HEARTBEAT_INTERVAL_S (default 180s)."""
    miss_count = int(os.environ.get("FOREMAN_STALE_MISS_COUNT", "3"))
    interval_s = int(os.environ.get("FOREMAN_HEARTBEAT_INTERVAL_S", "60"))
    return miss_count * interval_s
