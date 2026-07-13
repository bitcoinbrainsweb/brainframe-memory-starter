"""TUI screen-state contract for the Claude Code CLI PTY harness.

Contract version stamped against FOREMAN_CLI_VERSION. All matchers operate
on ANSI-stripped text. Matcher priority: rate_limited ranks above generic_error
so ambiguous frames in a rate-limit window resolve to rate_limited.
"""
from __future__ import annotations

import os
import re
from enum import Enum
from typing import NamedTuple

CONTRACT_VERSION: str = "1.0"
FOREMAN_CLI_VERSION: str = os.environ.get("FOREMAN_CLI_VERSION", "")

_ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from text."""
    return _ANSI_RE.sub("", text)


class ScreenState(str, Enum):
    PROMPT_READY = "prompt_ready"
    AGENT_THINKING = "agent_thinking"
    TURN_COMPLETE = "turn_complete"
    SESSION_END = "session_end"
    RATE_LIMITED = "rate_limited"
    AUTH_ERROR = "auth_error"
    MODEL_REJECTED = "model_rejected"
    PROCESS_EXITED = "process_exited"
    SESSION_ABORTED = "session_aborted"
    GENERIC_ERROR = "generic_error"


REQUIRED_STATES: frozenset[ScreenState] = frozenset(ScreenState)

TERMINAL_STATES: frozenset[ScreenState] = frozenset({
    ScreenState.TURN_COMPLETE,
    ScreenState.SESSION_END,
    ScreenState.RATE_LIMITED,
    ScreenState.AUTH_ERROR,
    ScreenState.MODEL_REJECTED,
    ScreenState.PROCESS_EXITED,
    ScreenState.SESSION_ABORTED,
    ScreenState.GENERIC_ERROR,
})

# Canonical text for each state used in tests and smoke check.
CANONICAL_TEXT: dict[ScreenState, str] = {
    ScreenState.PROMPT_READY: "> ",
    ScreenState.AGENT_THINKING: "Thinking...",
    ScreenState.TURN_COMPLETE: "Task complete\n> ",
    ScreenState.SESSION_END: "Bye!",
    ScreenState.RATE_LIMITED: "Rate limited. Please wait.",
    ScreenState.AUTH_ERROR: "authentication failed",
    ScreenState.MODEL_REJECTED: "Model not available. enforceAvailableModels",
    ScreenState.PROCESS_EXITED: "[Exited]",
    ScreenState.SESSION_ABORTED: "Session aborted",
    ScreenState.GENERIC_ERROR: "Error: something went wrong",
}


class ContractMatch(NamedTuple):
    state: ScreenState
    raw_text: str


# Ordered matchers: rate_limited MUST appear before generic_error.
# pexpect.expect() receives the compiled patterns list in this order.
_ORDERED_MATCHERS: list[tuple[ScreenState, re.Pattern]] = [
    (ScreenState.RATE_LIMITED, re.compile(
        r"rate.{0,10}limit|overloaded|too.{0,10}many.{0,10}requests|429", re.IGNORECASE
    )),
    (ScreenState.AUTH_ERROR, re.compile(
        r"authentication|auth.{0,10}error|invalid.{0,10}key|unauthorized|401", re.IGNORECASE
    )),
    (ScreenState.MODEL_REJECTED, re.compile(
        r"model.{0,30}not.{0,20}(?:available|found|allowed)|model.{0,20}rejected"
        r"|enforceAvailableModels", re.IGNORECASE
    )),
    (ScreenState.SESSION_ABORTED, re.compile(
        r"session.{0,15}abort|forcefully.{0,20}terminat", re.IGNORECASE
    )),
    (ScreenState.PROCESS_EXITED, re.compile(
        r"\[Exited\]|process.{0,15}exit(?:ed)?|child.{0,15}exit(?:ed)?", re.IGNORECASE
    )),
    (ScreenState.SESSION_END, re.compile(
        r"(?:^|\n)Bye[.!]|session.{0,15}end(?:ed)?|Goodbye", re.IGNORECASE
    )),
    (ScreenState.GENERIC_ERROR, re.compile(
        r"(?:^|\n)Error:|fatal:|Traceback|something.{0,10}went.{0,10}wrong", re.IGNORECASE
    )),
    (ScreenState.AGENT_THINKING, re.compile(
        r"[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]|Thinking\.\.\.|Working\.\.\.|Processing\.\.\.", re.IGNORECASE
    )),
    (ScreenState.TURN_COMPLETE, re.compile(
        r"[✓✔]|Task\s+complete|completed\s+successfully|esc\s+to\s+interrupt", re.IGNORECASE
    )),
    (ScreenState.PROMPT_READY, re.compile(
        r"(?:^|\r?\n)>\s*$|(?:^|\r?\n)>\s", re.MULTILINE
    )),
]

ORDERED_STATES: list[ScreenState] = [s for s, _ in _ORDERED_MATCHERS]
ORDERED_PATTERNS: list[re.Pattern] = [p for _, p in _ORDERED_MATCHERS]


def match_state(text: str) -> ContractMatch | None:
    """Match ANSI-stripped text against contract matchers. Returns first match or None."""
    stripped = strip_ansi(text)
    for state, pattern in _ORDERED_MATCHERS:
        if pattern.search(stripped):
            return ContractMatch(state=state, raw_text=text)
    return None


def rate_limited_before_generic_error() -> bool:
    """Verify matcher priority: rate_limited index < generic_error index."""
    rl_idx = ORDERED_STATES.index(ScreenState.RATE_LIMITED)
    ge_idx = ORDERED_STATES.index(ScreenState.GENERIC_ERROR)
    return rl_idx < ge_idx
