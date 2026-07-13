"""PTY harness for driving the builder CLI (the coding agent) as a subprocess.

Spawns the builder CLI (BUILDER_CLI_CMD) inside a PTY using pexpect, drives
prompt I/O, detects terminal screen states via tui_contract matchers, and
enforces logical-run budgets.

Budget model:
- turn budget (FOREMAN_AGENT_TURN_BUDGET, default 60): CUMULATIVE across
  re-dispatch attempts; a "turn" = one observed turn_complete transition.
- wall-clock budget (FOREMAN_AGENT_WALLCLOCK_BUDGET, default 1800):
  ACTIVE WORK ONLY, PAUSED during rate-limit backoff.
- re-dispatch cap (FOREMAN_AGENT_MAX_RESUMES, default 3).

Terminal reasons: turn-budget-exceeded, wallclock-budget-exceeded,
ratelimit-resumes-exhausted, cli-contract-broken, cli-crashed,
session-aborted, auth-error, model-rejected.

The model is passed via the CLI's model flag ONLY; the model is never switched
in-session because that can mutate the builder's process-global config.
"""
from __future__ import annotations

import os
import re
import signal
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

try:
    import pexpect
    HAS_PEXPECT = True
except ImportError:
    pexpect = None  # type: ignore[assignment]
    HAS_PEXPECT = False

# Windows: pexpect.spawn requires a real PTY (ptyprocess), which is Unix-only.
# Detect whether spawn is available; if not, fall back to popen_spawn + winpty.
_HAS_SPAWN = HAS_PEXPECT and hasattr(pexpect, "spawn")

from .tui_contract import (
    ORDERED_PATTERNS,
    ORDERED_STATES,
    ScreenState,
    strip_ansi,
)
from .watchdog import SessionWatchdog, make_process_tree_probes, supervise

BUILDER_CLI: str = os.environ.get("BUILDER_CLI_CMD", "your-builder-cli")  # the coding agent that writes code
FOREMAN_AGENT_TURN_BUDGET: int = int(os.environ.get("FOREMAN_AGENT_TURN_BUDGET", "60"))
FOREMAN_AGENT_WALLCLOCK_BUDGET: int = int(os.environ.get("FOREMAN_AGENT_WALLCLOCK_BUDGET", "1800"))
FOREMAN_AGENT_MAX_RESUMES: int = int(os.environ.get("FOREMAN_AGENT_MAX_RESUMES", "3"))
FOREMAN_TUI_UNKNOWN_TIMEOUT: int = int(os.environ.get("FOREMAN_TUI_UNKNOWN_TIMEOUT", "120"))
_KILL_GRACE_SECS: int = int(os.environ.get("FOREMAN_KILL_GRACE_SECS", "10"))
# Rate-limit backoff in seconds between re-dispatch attempts
_RATELIMIT_BACKOFF: int = int(os.environ.get("FOREMAN_RATELIMIT_BACKOFF", "30"))
# Per-spec aggregate wall-clock ceiling across all attempts of one task
FOREMAN_SPEC_WALLCLOCK_CEILING: int = int(os.environ.get("FOREMAN_SPEC_WALLCLOCK_CEILING", "2700"))
# Tighter wall-clock budget for verify agents (shorter than full build budget)
FOREMAN_VERIFY_WALLCLOCK_BUDGET: int = int(os.environ.get("FOREMAN_VERIFY_WALLCLOCK_BUDGET", "600"))


@dataclass
class LogicalRunContext:
    """Cumulative state across re-dispatch attempts of the same logical run."""

    run_id: str
    turns_used: int = 0
    resumes_used: int = 0
    wallclock_active_secs: float = 0.0
    turn_budget: int = field(default_factory=lambda: FOREMAN_AGENT_TURN_BUDGET)
    wallclock_budget: int = field(default_factory=lambda: FOREMAN_AGENT_WALLCLOCK_BUDGET)
    max_resumes: int = field(default_factory=lambda: FOREMAN_AGENT_MAX_RESUMES)

    def record_turn(self) -> None:
        self.turns_used += 1

    def record_active(self, secs: float) -> None:
        self.wallclock_active_secs += secs

    def record_resume(self) -> None:
        self.resumes_used += 1

    @property
    def turn_budget_exceeded(self) -> bool:
        return self.turns_used >= self.turn_budget

    @property
    def wallclock_budget_exceeded(self) -> bool:
        return self.wallclock_active_secs >= self.wallclock_budget

    @property
    def resumes_exhausted(self) -> bool:
        return self.resumes_used >= self.max_resumes


@dataclass
class HarnessResult:
    """Result from a PTY harness logical run."""

    raw_output: str
    terminal_reason: str
    turns_used: int
    wallclock_active_secs: float
    # Set when the watchdog killed a wedged / over-cap session (AC6):
    # wedge duration, last output excerpt, attempt elapsed.
    wedge_detail: dict | None = None


def _default_spawn_fn(
    command: str,
    args: list[str],
    cwd: str,
    env: dict[str, str],
) -> Any:
    if not HAS_PEXPECT:
        raise ImportError("pexpect is required for production PTY harness; install pexpect>=4.8.0")

    if not _HAS_SPAWN:
        # Windows: pexpect.spawn unavailable (ptyprocess is Unix-only).
        # The transport layer handles Windows via _run_print_mode (the builder CLI's print mode).
        # If _default_spawn_fn is somehow called on Windows, raise a clear error.
        raise RuntimeError(
            "pexpect.spawn is not available on this platform. "
            "CliAgentTransport._run_print_mode should be used on Windows instead."
        )

    return pexpect.spawn(
        command,
        args=args,
        cwd=cwd,
        env=env,
        encoding="utf-8",
        timeout=None,
    )


def _kill_process(process: Any, grace_secs: int = _KILL_GRACE_SECS) -> None:
    """SIGTERM then SIGKILL after grace period."""
    try:
        process.terminate()
    except Exception:
        pass
    deadline = time.monotonic() + grace_secs
    while time.monotonic() < deadline:
        try:
            if not process.isalive():
                return
        except Exception:
            return
        time.sleep(0.2)
    try:
        # SIGKILL is Unix-only; on Windows use SIGTERM (terminate()) which is already tried above.
        _SIGKILL = getattr(signal, "SIGKILL", getattr(signal, "SIGTERM", None))
        if _SIGKILL is not None:
            process.kill(_SIGKILL)
    except Exception:
        pass
    try:
        process.close()
    except Exception:
        pass


_STATE_TO_REASON: dict[ScreenState, str] = {
    ScreenState.TURN_COMPLETE: "turn_complete",
    ScreenState.SESSION_END: "session-end",
    ScreenState.RATE_LIMITED: "rate-limited",
    ScreenState.AUTH_ERROR: "auth-error",
    ScreenState.MODEL_REJECTED: "model-rejected",
    ScreenState.PROCESS_EXITED: "cli-crashed",
    ScreenState.SESSION_ABORTED: "session-aborted",
    ScreenState.GENERIC_ERROR: "generic-error",
}


class PtyHarness:
    """Drives the pexpect-spawned builder CLI for one logical run.

    Accepts an injectable spawn_fn so tests can replace pexpect.spawn
    with a MockPtyProcess that replays recorded TUI transcripts.
    """

    def __init__(
        self,
        spawn_fn: Callable | None = None,
        unknown_timeout: int = FOREMAN_TUI_UNKNOWN_TIMEOUT,
        ratelimit_backoff: int = _RATELIMIT_BACKOFF,
        kill_grace_secs: int = _KILL_GRACE_SECS,
        watchdog: SessionWatchdog | None = None,
        watchdog_step_s: float | None = None,
    ) -> None:
        self._spawn_fn = spawn_fn if spawn_fn is not None else _default_spawn_fn
        self._unknown_timeout = unknown_timeout
        self._ratelimit_backoff = ratelimit_backoff
        self._kill_grace_secs = kill_grace_secs
        # R2: optional liveness watchdog. When present, a supervision thread heartbeats
        # and kills a wedged / over-cap session; the read loop feeds it output activity.
        self._watchdog = watchdog
        self._watchdog_step_s = watchdog_step_s

    def run(
        self,
        prompt: str,
        model: str,
        working_dir: Path,
        env: dict[str, str],
        ctx: LogicalRunContext,
    ) -> HarnessResult:
        """Run a single dispatch attempt, updating ctx in place.

        NEVER issues /model in-session. Model passed via --model arg only
        (prevents writing to process-global settings.json).
        """
        raw_parts: list[str] = []
        terminal_reason: str = "unknown"

        process = self._spawn_fn(
            BUILDER_CLI,
            ["--model", model, "--dangerously-skip-permissions"],
            str(working_dir),
            {**env, "FOREMAN_ORCHESTRATED": "1"},
        )

        # R2: launch the liveness supervision thread (heartbeat + wedge/wall-clock kill).
        # It lives inside this session-supervision loop -- no separate process.
        _wd = self._watchdog
        _wd_stop = threading.Event()
        _wd_thread = None
        if _wd is not None:
            _wd.start()
            # attach the process-tree CPU probe now that the child exists,
            # so silent-but-working sessions (long test suites inside one tool
            # call) are not killed as wedged. Degrades to output-only when no
            # backend exists on this platform.
            try:
                child_pid = int(getattr(process, "pid", 0) or 0)
                if child_pid:
                    _cpu_probe, _tree_probe = make_process_tree_probes(child_pid)
                    if _cpu_probe is not None:
                        _wd.set_cpu_probe(_cpu_probe, _tree_probe)
            except Exception:
                pass

            def _is_alive() -> bool:
                try:
                    return bool(process.isalive())
                except Exception:
                    return False

            def _kill() -> None:
                _kill_process(process, self._kill_grace_secs)

            _wd_thread = threading.Thread(
                target=supervise,
                args=(_wd, _is_alive, _kill),
                kwargs={"step_s": self._watchdog_step_s, "stop_event": _wd_stop},
                daemon=True,
                name="foreman-watchdog",
            )
            _wd_thread.start()

        try:
            attempt_start = time.monotonic()

            # Wait for initial prompt_ready
            self._expect_prompt_ready(process, raw_parts)

            # Send the task prompt
            process.sendline(prompt)

            # Main read loop
            while True:
                # R2: watchdog kill takes precedence over any other terminal reason.
                if _wd is not None and _wd.fired_reason:
                    terminal_reason = _wd.fired_reason
                    break

                # Check wall-clock budget before blocking
                if ctx.wallclock_budget_exceeded:
                    _kill_process(process, self._kill_grace_secs)
                    terminal_reason = "wallclock-budget-exceeded"
                    break

                loop_start = time.monotonic()
                state, text = self._read_next_state(process, raw_parts)
                loop_elapsed = time.monotonic() - loop_start

                # any output bytes reset the wedge clock and drive a
                # throttled heartbeat.
                if _wd is not None:
                    _wd.note_output(text or "")

                if state is None:
                    # TIMEOUT: unknown state for unknown_timeout seconds
                    _kill_process(process, self._kill_grace_secs)
                    terminal_reason = "cli-contract-broken"
                    break

                # Track active time (not during rate-limit backoff)
                ctx.record_active(loop_elapsed)

                if state == ScreenState.AGENT_THINKING:
                    continue

                if state == ScreenState.PROMPT_READY:
                    # Intermediate prompt_ready (not a turn boundary by itself)
                    continue

                if state == ScreenState.TURN_COMPLETE:
                    ctx.record_turn()
                    terminal_reason = "turn_complete"
                    if ctx.turn_budget_exceeded:
                        _kill_process(process, self._kill_grace_secs)
                        terminal_reason = "turn-budget-exceeded"
                    break

                if state == ScreenState.RATE_LIMITED:
                    # Pause wall-clock during backoff
                    _kill_process(process, self._kill_grace_secs)
                    ctx.record_resume()
                    if ctx.resumes_exhausted:
                        terminal_reason = "ratelimit-resumes-exhausted"
                        break
                    # Backoff then respawn (caller will loop on this method)
                    time.sleep(self._ratelimit_backoff)
                    terminal_reason = "rate-limited-respawn"
                    break

                # All other terminal states
                _kill_process(process, self._kill_grace_secs)
                terminal_reason = _STATE_TO_REASON.get(state, state.value)
                break

        finally:
            _wd_stop.set()
            if _wd_thread is not None:
                _wd_thread.join(timeout=2)
            try:
                if process.isalive():
                    _kill_process(process, self._kill_grace_secs)
            except Exception:
                pass
            try:
                process.close()
            except Exception:
                pass

        # R2: a watchdog fire overrides the terminal reason (a killed wedge otherwise
        # surfaces as PROCESS_EXITED / cli-contract-broken) and attaches the trail.
        wedge_detail = None
        if _wd is not None and _wd.fired_reason:
            terminal_reason = _wd.fired_reason
            wedge_detail = _wd.wedge_detail(_wd.fired_reason)

        return HarnessResult(
            raw_output="\n".join(raw_parts),
            terminal_reason=terminal_reason,
            turns_used=ctx.turns_used,
            wallclock_active_secs=ctx.wallclock_active_secs,
            wedge_detail=wedge_detail,
        )

    def _expect_prompt_ready(self, process: Any, raw_parts: list[str]) -> None:
        """Wait for the initial prompt_ready state."""
        if not HAS_PEXPECT:
            # Mock mode: first expect() call returns PROMPT_READY
            state, text = self._read_next_state(process, raw_parts)
            return
        state, text = self._read_next_state(process, raw_parts)

    def _read_next_state(
        self, process: Any, raw_parts: list[str]
    ) -> tuple[ScreenState | None, str]:
        """Read from the process until a known state is detected.

        Returns (ScreenState, matched_text) or (None, "") on timeout.
        Unknown state is NEVER success.
        """
        if HAS_PEXPECT:
            eof_cls = pexpect.EOF
            timeout_cls = pexpect.TIMEOUT
        else:
            # In mock-only mode, define placeholder exceptions
            class _FakeEOF(Exception):
                pass
            class _FakeTimeout(Exception):
                pass
            eof_cls = _FakeEOF
            timeout_cls = _FakeTimeout

        try:
            idx = process.expect(ORDERED_PATTERNS, timeout=self._unknown_timeout)
            text = getattr(process, "before", "") or ""
            raw_parts.append(text)
            return ORDERED_STATES[idx], text
        except eof_cls:
            return ScreenState.PROCESS_EXITED, ""
        except timeout_cls:
            return None, ""
        except Exception as exc:
            # Check by name for mock environments without real pexpect
            cls_name = type(exc).__name__
            if "EOF" in cls_name:
                return ScreenState.PROCESS_EXITED, ""
            if "TIMEOUT" in cls_name or "Timeout" in cls_name:
                return None, ""
            raise


def run_with_resumes(
    harness: PtyHarness,
    prompt: str,
    model: str,
    working_dir: Path,
    env: dict[str, str],
    ctx: LogicalRunContext,
) -> HarnessResult:
    """Run with automatic resume on rate-limit, honouring resume cap."""
    last_result: HarnessResult | None = None
    while True:
        result = harness.run(prompt, model, working_dir, env, ctx)
        last_result = result
        if result.terminal_reason == "rate-limited-respawn":
            # Already counted in ctx.resumes_used by harness.run()
            continue
        break
    return last_result  # type: ignore[return-value]
