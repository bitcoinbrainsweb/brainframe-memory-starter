"""CI gate for Foreman runner.

Called after verify PASS, before ff-merge. Polls GitHub Check Runs API.
Transition sequence: verifying -> ci-gating -> merging (pass) | parked (fail/timeout/api-error).

CIGate.poll() is pure -- no DB writes. The caller (runner or bundle runner) handles write-ahead
transitions to ci-gating (before poll) and ci_verdict persistence (before merge transition).
This matches the existing runner.py pattern of explicit write-ahead before every action.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from enum import Enum


class CIOutcome(str, Enum):
    PASS = "pass"
    PASS_NO_CI = "pass-no-ci"
    FAIL = "fail"
    TIMEOUT = "timeout"
    API_ERROR = "api-error"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _github_get(url: str, token: str, timeout: int = 30) -> tuple[int, dict | None]:
    """GET a GitHub API URL. Returns (status_code, parsed_body | None).

    status_code=0 means network/connection error.
    """
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode())
            return resp.status, body
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode())
        except Exception:
            body = None
        return exc.code, body
    except Exception:
        return 0, None


class CIGate:
    """Poll GitHub Check Runs and gate on all checks passing.

    Usage (caller handles write-ahead):
        gate = CIGate()

        # Write-ahead: ci-gating before polling
        ledger.transition(..., new_status="ci-gating", prior_status="verifying", ...)

        outcome, verdict = gate.poll(owner, repo, branch_sha)

        # Write ci_verdict before merge transition
        ledger.update_data(run_uuid, spec_slug, {"ci_verdict": verdict})

        if outcome in (CIOutcome.PASS, CIOutcome.PASS_NO_CI):
            ledger.transition(..., new_status="merging", prior_status="ci-gating", ...)
        else:
            park_reason = _park_reason_for(outcome)
            ledger.transition(..., new_status="parked", park_reason=park_reason, ...)
    """

    def __init__(self) -> None:
        self._token = os.environ.get("GITHUB_PRIMARY_PAT", "")
        self._ci_timeout_s = int(os.environ.get("FOREMAN_CI_TIMEOUT_S", "600"))
        self._transient_retries = 5
        self._transient_delay_s = 10
        self._registration_wait_s = 60
        self._backoff_initial_s = 30
        self._backoff_max_s = 120

    def poll(
        self,
        owner: str,
        repo: str,
        branch_sha: str,
    ) -> tuple[CIOutcome, dict]:
        """Poll GitHub Check Runs for branch_sha. Returns (outcome, ci_verdict).

        ci_verdict schema:
          {"ci_configured": bool, "checks": [{"name": str, "conclusion": str}],
           "conclusion": "success|failure|timeout|api-error|pass-no-ci", "timestamp": "<iso>"}

        No DB writes -- caller is responsible for write-ahead and verdict persistence.
        """
        url = f"https://api.github.com/repos/{owner}/{repo}/commits/{branch_sha}/check-runs"
        return self._run_poll(url, branch_sha)

    def _run_poll(self, url: str, sha: str) -> tuple[CIOutcome, dict]:
        # --- Registration wait: up to 60s for at least one check-run to appear ---
        deadline_reg = time.monotonic() + self._registration_wait_s
        while time.monotonic() < deadline_reg:
            status_code, body = self._get_with_retry(url)
            if status_code == 0:
                return self._api_error_verdict()
            if status_code == 404:
                # "No checks yet"during registration wait
                time.sleep(5)
                continue
            if status_code >= 500:
                return self._api_error_verdict()
            if status_code >= 400:
                return self._non_retriable_4xx_verdict(status_code)
            runs = (body or {}).get("check_runs", [])
            if runs:
                break
            time.sleep(5)
        else:
            # No CI configured -- auto-pass
            print(
                f"[ci-gate] WARNING: no check-runs after {self._registration_wait_s}s "
                f"for sha={sha!r}; treating as no-CI (pass-no-ci)",
                file=sys.stderr,
            )
            return CIOutcome.PASS_NO_CI, {
                "ci_configured": False,
                "checks": [],
                "conclusion": "pass-no-ci",
                "timestamp": _now_iso(),
            }

        # --- Main poll loop: exponential backoff, total budget = FOREMAN_CI_TIMEOUT_S ---
        deadline_total = time.monotonic() + self._ci_timeout_s
        backoff = self._backoff_initial_s
        while time.monotonic() < deadline_total:
            status_code, body = self._get_with_retry(url)
            if status_code == 0:
                return self._api_error_verdict()
            if status_code >= 500:
                return self._api_error_verdict()
            if status_code >= 400 and status_code != 404:
                return self._non_retriable_4xx_verdict(status_code)

            runs = (body or {}).get("check_runs", [])
            checks = [
                {
                    "name": r["name"],
                    "conclusion": r.get("conclusion") or "",
                    "status": r.get("status", ""),
                }
                for r in runs
            ]

            incomplete = [c for c in checks if c["status"] != "completed"]
            if incomplete:
                sleep_s = min(backoff, self._backoff_max_s, max(0.1, deadline_total - time.monotonic()))
                time.sleep(sleep_s)
                backoff = min(backoff * 2, self._backoff_max_s)
                continue

            # All completed -- evaluate conclusions
            verdict_checks = [{"name": c["name"], "conclusion": c["conclusion"]} for c in checks]
            failing = [c for c in checks if c["conclusion"] not in ("success", "neutral", "skipped")]
            if failing:
                return CIOutcome.FAIL, {
                    "ci_configured": True,
                    "checks": verdict_checks,
                    "conclusion": "failure",
                    "timestamp": _now_iso(),
                }
            return CIOutcome.PASS, {
                "ci_configured": True,
                "checks": verdict_checks,
                "conclusion": "success",
                "timestamp": _now_iso(),
            }

        # Budget exhausted
        return CIOutcome.TIMEOUT, {
            "ci_configured": True,
            "checks": [],
            "conclusion": "timeout",
            "timestamp": _now_iso(),
        }

    def _get_with_retry(self, url: str) -> tuple[int, dict | None]:
        """GET with transient retry: 5xx and network errors up to 5x at 10s delay."""
        last_code, last_body = 0, None
        for attempt in range(self._transient_retries + 1):
            status_code, body = _github_get(url, self._token)
            if status_code not in (0,) and status_code < 500:
                return status_code, body
            last_code, last_body = status_code, body
            if attempt < self._transient_retries:
                time.sleep(self._transient_delay_s)
        return last_code, last_body

    def _api_error_verdict(self) -> tuple[CIOutcome, dict]:
        return CIOutcome.API_ERROR, {
            "ci_configured": True,
            "checks": [],
            "conclusion": "api-error",
            "timestamp": _now_iso(),
        }

    def _non_retriable_4xx_verdict(self, status_code: int) -> tuple[CIOutcome, dict]:
        return CIOutcome.API_ERROR, {
            "ci_configured": True,
            "checks": [],
            "conclusion": "api-error",
            "timestamp": _now_iso(),
            "http_status": status_code,
        }


def park_reason_for_ci_outcome(outcome: CIOutcome) -> str:
    """Map CIOutcome to a foreman_tasks.park_reason string."""
    return {
        CIOutcome.FAIL: "ci-failed",
        CIOutcome.TIMEOUT: "ci-timeout",
        CIOutcome.API_ERROR: "ci-gate-api-error",
    }.get(outcome, "ci-failed")
