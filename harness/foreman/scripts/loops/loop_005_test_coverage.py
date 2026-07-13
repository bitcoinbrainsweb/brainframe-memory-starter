"""Foreman Loop 005 - 100% Test Coverage dispatch orchestrator.

R1: Iterative coverage loop; each iteration is a single agent invocation.
R2: Foreman owns re-dispatch; no inter-iteration delay; stall detection.
R3: Wall-clock ceiling enforced by Foreman; branch pushed before PARTIAL.
R4: On success, push then open PR targeting detected default_branch.
"""
from __future__ import annotations

import re
import time
from typing import Callable

from scripts.foreman.prompts import render_loop_005_prompt

_EXIT_SUCCESS_RE = re.compile(r"\bEXIT\s+SUCCESS\b")
_EXIT_ITERATION_RE = re.compile(
    r"\bEXIT\s+ITERATION_COMPLETE\s+global_coverage=([0-9.]+)(?:\s+target_file=(\S+))?"
)
_PARTIAL_UNSUPPORTED_RE = re.compile(r"\bPARTIAL-UNSUPPORTED\s+reason=\"([^\"]*)\"")

# values >= 99.995% are treated as 100%
_COVERAGE_ROUNDING_THRESHOLD = 99.995

# file skipped after this many consecutive zero-delta iterations
_STALL_LIMIT = 2


def select_target_file(per_file: dict[str, int], stalled: set[str]) -> str | None:
    """Select file with maximum uncovered lines; tie-break by lexicographic path ascending.

    Excludes stalled files. Returns None when no eligible files remain.
    tie-break is lexicographic ascending (alphabetically earlier path wins).
    """
    eligible = [(path, count) for path, count in per_file.items()
                if count > 0 and path not in stalled]
    if not eligible:
        return None
    eligible.sort(key=lambda x: (-x[1], x[0]))
    return eligible[0][0]


def _default_get_default_branch(target_repo: str) -> str:
    import subprocess
    result = subprocess.run(
        ["gh", "api", f"repos/{target_repo}", "--jq", ".default_branch"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return "main"
    return result.stdout.strip()


def _default_push_branch(branch_name: str) -> None:
    import subprocess
    subprocess.run(
        ["git", "push", "-u", "origin", branch_name],
        check=True,
        capture_output=True,
    )


def _default_open_pr(
    target_repo: str,
    branch_name: str,
    default_branch: str,
    before_coverage: float,
    after_coverage: float,
    files_added: list,
) -> None:
    import subprocess
    files_list = "\n".join(f"- `{f}`" for f in files_added) or "_(none recorded)_"
    body = (
        f"Before: {before_coverage:.2f}%  After: {after_coverage:.2f}%\n\n"
        f"## Test files added\n\n{files_list}\n"
    )
    subprocess.run(
        [
            "gh", "pr", "create",
            "--repo", target_repo,
            "--base", default_branch,
            "--head", branch_name,
            "--title", "chore: achieve coverage threshold via coverage loop",
            "--body", body,
        ],
        check=True,
        capture_output=True,
    )


def _is_at_threshold(coverage: float, threshold: float) -> bool:
    """True when coverage meets the threshold; >= 99.995% always counts as success."""
    effective = min(threshold, _COVERAGE_ROUNDING_THRESHOLD)
    return coverage >= effective


def dispatch(
    payload: dict,
    run_id: str,
    *,
    invoke_agent: Callable[[str], str] | None = None,
    get_default_branch: Callable[[str], str] | None = None,
    push_branch_fn: Callable[[str], None] | None = None,
    open_pr_fn: Callable[..., None] | None = None,
    monotonic_fn: Callable[[], float] | None = None,
) -> dict:
    """Orchestrate loop 005 iterations until threshold, ceiling, or stall.

    Each iteration is one agent invocation. Foreman owns re-dispatch.

    Injectable callables (all have real defaults; supply mocks in tests):
      invoke_agent(prompt) -> raw_output
      get_default_branch(target_repo) -> branch_name
      push_branch_fn(branch_name) -> None
      open_pr_fn(**kwargs) -> None   kwargs: target_repo, branch_name, default_branch,
                                             before_coverage, after_coverage, files_added
      monotonic_fn() -> float  (wall-clock seconds, monotonically increasing)

    Returns dict with keys: status, reason, no_op, branch, before_coverage,
                            after_coverage, files_added.
    """
    target_repo = payload["target_repo"]
    coverage_threshold = float(payload.get("coverage_threshold", 100.0))
    ceiling_minutes = float(payload.get("wall_clock_ceiling_minutes", 60.0))
    branch_prefix = payload.get("branch_prefix", "coverage-loop")

    _get_branch = get_default_branch or _default_get_default_branch
    _push = push_branch_fn or _default_push_branch
    _pr = open_pr_fn or _default_open_pr
    _clock = monotonic_fn or time.monotonic

    default_branch = _get_branch(target_repo)
    branch_name = f"{branch_prefix}/{run_id}"
    ceiling_secs = ceiling_minutes * 60.0
    start = _clock()

    before_coverage: float | None = None
    current_coverage: float | None = None
    stall_tracker: dict[str, int] = {}
    stalled_files: set[str] = set()
    files_added: list[str] = []
    attempt = 0

    while True:
        # ceiling check before each agent dispatch
        if _clock() - start >= ceiling_secs:
            _push(branch_name)  # push before PARTIAL
            return {
                "status": "PARTIAL",
                "reason": "ceiling: wall-clock budget exhausted",
                "branch": branch_name,
                "before_coverage": before_coverage,
                "after_coverage": current_coverage,
                "files_added": files_added,
            }

        prompt = render_loop_005_prompt(
            spec=payload,
            run_id=run_id,
            branch_name=branch_name,
            default_branch=default_branch,
            attempt=attempt,
            before_coverage=before_coverage,
            current_coverage=current_coverage,
            stalled_files=stalled_files,
        )

        if invoke_agent is None:
            return {"status": "ERROR", "reason": "no invoke_agent provided"}

        raw_output = invoke_agent(prompt)

        # PARTIAL-UNSUPPORTED: agent cannot extract per-file coverage data
        m_partial = _PARTIAL_UNSUPPORTED_RE.search(raw_output)
        if m_partial:
            return {"status": "PARTIAL-UNSUPPORTED", "reason": m_partial.group(1)}

        # EXIT SUCCESS
        if _EXIT_SUCCESS_RE.search(raw_output):
            if before_coverage is None:
                # EXIT SUCCESS before any ITERATION_COMPLETE = no-op
                return {
                    "status": "SUCCESS",
                    "no_op": True,
                    "reason": "no-op: coverage already meets threshold",
                    "branch": branch_name,
                    "before_coverage": None,
                    "after_coverage": None,
                    "files_added": [],
                }
            # Genuine success: push then open PR (push precedes open_pr)
            _push(branch_name)
            _pr(
                target_repo=target_repo,
                branch_name=branch_name,
                default_branch=default_branch,
                before_coverage=before_coverage,
                after_coverage=current_coverage or before_coverage,
                files_added=files_added,
            )
            return {
                "status": "SUCCESS",
                "branch": branch_name,
                "before_coverage": before_coverage,
                "after_coverage": current_coverage,
                "files_added": files_added,
            }

        # EXIT ITERATION_COMPLETE
        m_iter = _EXIT_ITERATION_RE.search(raw_output)
        if m_iter:
            new_coverage = float(m_iter.group(1))
            target_file = m_iter.group(2) or ""

            if before_coverage is None:
                before_coverage = new_coverage

            prev = current_coverage if current_coverage is not None else before_coverage
            delta = new_coverage - prev

            # Check threshold after iteration (defensive: agent may miss the exit condition)
            if _is_at_threshold(new_coverage, coverage_threshold):
                _push(branch_name)
                _pr(
                    target_repo=target_repo,
                    branch_name=branch_name,
                    default_branch=default_branch,
                    before_coverage=before_coverage,
                    after_coverage=new_coverage,
                    files_added=files_added,
                )
                return {
                    "status": "SUCCESS",
                    "branch": branch_name,
                    "before_coverage": before_coverage,
                    "after_coverage": new_coverage,
                    "files_added": files_added,
                }

            # Track progress and stall state per file
            if target_file:
                if delta == 0.0:
                    stall_tracker[target_file] = stall_tracker.get(target_file, 0) + 1
                else:
                    stall_tracker[target_file] = 0
                    if target_file not in files_added:
                        files_added.append(target_file)

                if stall_tracker.get(target_file, 0) >= _STALL_LIMIT:
                    stalled_files.add(target_file)

                # all remaining files stalled -- emit PARTIAL
                if target_file in stalled_files and delta == 0.0:
                    return {
                        "status": "PARTIAL",
                        "reason": "stall: no coverable progress remaining",
                        "branch": branch_name,
                        "before_coverage": before_coverage,
                        "after_coverage": new_coverage,
                        "files_added": files_added,
                    }

            current_coverage = new_coverage
            attempt += 1
            continue

        # Unexpected agent output
        return {
            "status": "ERROR",
            "reason": f"agent returned unexpected output (attempt {attempt})",
            "raw_output": raw_output[:500],
        }
