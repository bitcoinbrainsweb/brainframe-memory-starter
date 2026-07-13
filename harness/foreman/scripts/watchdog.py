"""SessionWatchdog: builder/verifier liveness heartbeat + wedge watchdog.

The orchestrator had no liveness signal from a builder or verifier session and no
way to kill a wedged one: run fm-20260707-1750-7f12c5 had a builder session hang for
12.5 hours with the orchestrator blind to it, then a second spec entered the identical
hang. This module closes that gap.

Heartbeat is driven by transport output activity (any bytes counts) with a
pid-checked timer fallback, throttled to at most one write per interval. A session that
emits no output for the wedge threshold while its process is alive or that
exceeds the hard per-attempt wall-clock cap signals a kill; the two reasons are
handled identically downstream. Heartbeat writes are non-blocking and failure-tolerant
. The watchdog lives inside the existing session-supervision loop; there is no
separate health-agent process.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from typing import Callable

# Terminal reasons the transport translates into a builder-wedged disposition. Both are
# handled identically by bundle_runner: retry once, then park.
WEDGE_REASONS = frozenset({"builder-wedged", "attempt-wallclock-exceeded"})

# After this many consecutive heartbeat-write failures, the fallback backs off to avoid
# hammering a downed database every tick.
_FAILURE_BACKOFF_COUNT = 3


def wedge_threshold_s() -> int:
    """No-output wedge threshold in seconds (default 10 minutes)."""
    try:
        return int(os.environ.get("FOREMAN_WEDGE_THRESHOLD_SECONDS", "600"))
    except (ValueError, TypeError):
        return 600


def attempt_max_s() -> int:
    """Hard per-attempt wall-clock cap in seconds (default 90 minutes)."""
    try:
        return int(os.environ.get("FOREMAN_ATTEMPT_MAX_SECONDS", "5400"))
    except (ValueError, TypeError):
        return 5400


def heartbeat_interval_s() -> int:
    """Heartbeat cadence in seconds (default 60)."""
    try:
        return int(os.environ.get("FOREMAN_HEARTBEAT_INTERVAL_S", "60"))
    except (ValueError, TypeError):
        return 60


def watchdog_step_s() -> float:
    """Supervision poll step in seconds. Small enough to detect a wedge promptly, large
    enough not to busy-wait."""
    try:
        return float(os.environ.get("FOREMAN_WATCHDOG_STEP_SECONDS", "5"))
    except (ValueError, TypeError):
        return 5.0


class SessionWatchdog:
    """Liveness + wedge policy for a single agent session (build or verify).

    Pure decision object (injectable clock, thread-safe). The supervision loop feeds it
    output via note_output(), ticks the timer fallback via tick(), and asks
    overdue_reason() whether the session must be killed.
    """

    def __init__(
        self,
        heartbeat_sink: Callable[[], None] | None = None,
        *,
        wedge_threshold_s: int | None = None,
        attempt_max_s: int | None = None,
        interval_s: int | None = None,
        clock: Callable[[], float] = time.monotonic,
        cpu_probe: Callable[[], float] | None = None,
        tree_size_probe: Callable[[], int] | None = None,
    ) -> None:
        self._sink = heartbeat_sink
        self._wedge = wedge_threshold_s if wedge_threshold_s is not None else globals()["wedge_threshold_s"]()
        self._cap = attempt_max_s if attempt_max_s is not None else globals()["attempt_max_s"]()
        self._interval = interval_s if interval_s is not None else heartbeat_interval_s()
        self._clock = clock
        self._lock = threading.Lock()

        # process-tree CPU activity counts as life. Optional; None keeps
        # the original output-only semantics.
        self._cpu_probe = cpu_probe
        self._tree_size_probe = tree_size_probe
        self._cpu_epsilon = 0.05
        self._last_cpu_sample: float | None = None
        self._first_cpu_sample: float | None = None
        self._last_cpu_advance_at: float | None = None
        self._cpu_probe_broken = False

        self._start: float | None = None
        self._last_output: float | None = None
        self._last_output_text: str = ""
        self._last_hb: float | None = None
        self._hb_failures: int = 0
        self._hb_writes: int = 0
        # Set by supervise() when it fires a kill; read back by the harness to override
        # the terminal reason.
        self.fired_reason: str | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Mark the attempt start; the wall-clock cap and wedge clocks run from here."""
        with self._lock:
            now = self._clock()
            self._start = now
            self._last_output = now
            self._last_hb = None
            self.fired_reason = None
        self._sample_cpu()

    def set_cpu_probe(
        self,
        cpu_probe: Callable[[], float] | None,
        tree_size_probe: Callable[[], int] | None = None,
    ) -> None:
        """Attach the process-tree CPU probe after the child exists."""
        with self._lock:
            self._cpu_probe = cpu_probe
            self._tree_size_probe = tree_size_probe
            self._cpu_probe_broken = False
            self._last_cpu_sample = None
            self._first_cpu_sample = None
        self._sample_cpu()

    def _sample_cpu(self) -> None:
        """Sample the probe; on advance beyond epsilon, reset the CPU-staleness clock.
        A raising probe disables CPU gating (output-only fallback) rather than crashing."""
        probe = self._cpu_probe
        if probe is None or self._cpu_probe_broken:
            return
        try:
            sample = float(probe())
        except Exception:
            with self._lock:
                self._cpu_probe_broken = True
            return
        now = self._clock()
        with self._lock:
            if self._last_cpu_sample is None:
                self._last_cpu_sample = sample
                self._first_cpu_sample = sample
                self._last_cpu_advance_at = now
                return
            if sample - self._last_cpu_sample >= self._cpu_epsilon:
                self._last_cpu_advance_at = now
                self._last_cpu_sample = sample

    # ------------------------------------------------------------------
    # Output activity
    # ------------------------------------------------------------------

    def note_output(self, text: str = "") -> None:
        """Record output bytes: resets the wedge clock and drives a throttled heartbeat."""
        with self._lock:
            self._last_output = self._clock()
            if text:
                # Keep only a trailing window for the failure_trail excerpt.
                self._last_output_text = (self._last_output_text + text)[-2000:]
        self._heartbeat()

    def tick(self, alive: bool) -> None:
        """Timer-fallback heartbeat while the process is confirmed alive."""
        if alive:
            self._heartbeat()

    # ------------------------------------------------------------------
    # Heartbeat write
    # ------------------------------------------------------------------

    def _heartbeat(self) -> None:
        now = self._clock()
        with self._lock:
            interval = self._interval
            if self._hb_failures >= _FAILURE_BACKOFF_COUNT:
                # Back off after repeated failures so we do not hammer a downed DB.
                interval = self._interval * 2
            due = self._last_hb is None or (now - self._last_hb) >= interval
        if not due:
            return
        if self._sink is None:
            with self._lock:
                self._last_hb = now
            return
        try:
            self._sink()
        except Exception as exc: # never interrupt the build
            with self._lock:
                self._hb_failures += 1
                first = self._hb_failures == 1
                # Advance the throttle clock even on failure so a downed DB does not
                # get retried on every single tick.
                self._last_hb = now
            if first:
                print(f"[watchdog] heartbeat write failed (throttling further): {exc}",
                      file=sys.stderr)
            return
        with self._lock:
            self._last_hb = now
            self._hb_writes += 1
            self._hb_failures = 0

    # ------------------------------------------------------------------
    # Wedge / wall-clock decision
    # ------------------------------------------------------------------

    def overdue_reason(self, alive: bool) -> str | None:
        """Return the kill reason, or None. The wall-clock cap ignores liveness and
        output activity; the wedge requires an alive, silent process."""
        self._sample_cpu()
        now = self._clock()
        with self._lock:
            start, last_out = self._start, self._last_output
            cpu_gating = self._cpu_probe is not None and not self._cpu_probe_broken
            last_cpu_advance = self._last_cpu_advance_at
        if start is not None and (now - start) >= self._cap:
            return "attempt-wallclock-exceeded"
        if alive and last_out is not None and (now - last_out) >= self._wedge:
            # an alive, silent process whose tree is still burning CPU is
            # working, not wedged. Only kill when BOTH output and CPU are stale.
            if cpu_gating and last_cpu_advance is not None and (now - last_cpu_advance) < self._wedge:
                return None
            return "builder-wedged"
        return None

    def wedge_detail(self, reason: str) -> dict:
        """Detail block for failure_trail: wedge duration + last output excerpt."""
        now = self._clock()
        tree_size: int | None = None
        if self._tree_size_probe is not None:
            try:
                tree_size = int(self._tree_size_probe())
            except Exception:
                tree_size = None
        with self._lock:
            wedge_secs = round(now - self._last_output, 1) if self._last_output is not None else None
            elapsed = round(now - self._start, 1) if self._start is not None else None
            excerpt = self._last_output_text[-1000:]
            cpu_delta = (
                round(self._last_cpu_sample - self._first_cpu_sample, 2)
                if self._last_cpu_sample is not None and self._first_cpu_sample is not None
                else None
            )
        return {
            "wedge_reason": reason,
            "wedge_seconds": wedge_secs,
            "silence_seconds": wedge_secs,
            "attempt_elapsed_seconds": elapsed,
            "cpu_delta": cpu_delta,
            "proc_tree_size": tree_size,
            "last_output_excerpt": excerpt,
        }

    # ------------------------------------------------------------------
    # Introspection (run-report note)
    # ------------------------------------------------------------------

    @property
    def heartbeat_failures(self) -> int:
        with self._lock:
            return self._hb_failures

    @property
    def heartbeat_writes(self) -> int:
        with self._lock:
            return self._hb_writes


def supervise(
    watchdog: SessionWatchdog,
    is_alive: Callable[[], bool],
    kill: Callable[[], None],
    *,
    step_s: float | None = None,
    stop_event: threading.Event | None = None,
    _max_iters: int | None = None,
) -> None:
    """Background supervision loop: tick the heartbeat and kill on wedge / wall-clock.

    Runs as an ordinary thread inside the orchestrator's session-supervision loop; there
    is no separate health-agent process. Returns when the process dies, the
    watchdog fires a kill, or stop_event is set.
    """
    stop = stop_event if stop_event is not None else threading.Event()
    step = step_s if step_s is not None else watchdog_step_s()
    iters = 0
    while not stop.wait(step):
        iters += 1
        alive = False
        try:
            alive = bool(is_alive())
        except Exception:
            alive = False
        if not alive:
            # Process gone: nothing left to supervise. The read loop reports the exit.
            return
        watchdog.tick(alive)
        reason = watchdog.overdue_reason(alive)
        if reason:
            watchdog.fired_reason = reason
            try:
                kill()
            except Exception:
                pass
            return
        if _max_iters is not None and iters >= _max_iters:
            return


def make_process_tree_probes(root_pid: int):
    """Return (cpu_probe, tree_size_probe) for the process tree rooted at root_pid,
    or (None, None) when no measurement backend exists on this platform (
    degrade: caller keeps output-only wedge semantics).

    Backends: psutil when importable (all platforms, including Windows hosts),
    else /proc scanning (Linux). Probes raise on a vanished root; SessionWatchdog
    treats a raising probe as broken and falls back to output-only gating.
    """
    try:
        import psutil  # type: ignore
    except Exception:
        psutil = None

    if psutil is not None:
        def _tree(root=root_pid):
            root_proc = psutil.Process(root)
            return [root_proc] + root_proc.children(recursive=True)

        def cpu_probe() -> float:
            total = 0.0
            for proc in _tree():
                try:
                    t = proc.cpu_times()
                    total += float(t.user) + float(t.system)
                except Exception:
                    continue
            return total

        def tree_size_probe() -> int:
            return len(_tree())

        return cpu_probe, tree_size_probe

    proc_root = os.path.join("/", "proc")
    if os.path.isdir(proc_root):
        def _descendants(root=root_pid):
            children_map: dict[int, list[int]] = {}
            for name in os.listdir(proc_root):
                if not name.isdigit():
                    continue
                try:
                    with open(os.path.join(proc_root, name, "stat")) as fh:
                        parts = fh.read().split()
                    ppid = int(parts[3])
                except Exception:
                    continue
                children_map.setdefault(ppid, []).append(int(name))
            tree, stack = [root], [root]
            while stack:
                pid = stack.pop()
                for child in children_map.get(pid, []):
                    tree.append(child)
                    stack.append(child)
            return tree

        def cpu_probe() -> float:
            tick = os.sysconf("SC_CLK_TCK")
            total = 0.0
            found_root = False
            for pid in _descendants():
                try:
                    with open(os.path.join(proc_root, str(pid), "stat")) as fh:
                        parts = fh.read().split()
                    total += (int(parts[13]) + int(parts[14])) / tick
                    if pid == root_pid:
                        found_root = True
                except Exception:
                    continue
            if not found_root:
                raise ProcessLookupError(f"pid {root_pid} gone")
            return total

        def tree_size_probe() -> int:
            return len(_descendants())

        return cpu_probe, tree_size_probe

    return None, None
