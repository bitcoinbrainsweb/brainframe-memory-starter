"""Ground-truth precondition gates for Phase 2 (R9.AC2, R9.AC3, R9.AC5).

H-001: service-role credential NEVER appears in runner, agent context, or logs.
SupabaseInvariantHarness uses anon key only; real credential wiring is Phase 3.
"""
from __future__ import annotations

import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Protocol, runtime_checkable

from scripts.foreman.antislop_lint import run_antislop_lint
from scripts.foreman.models import (
    AntislopConfig,
    InvariantResult,
    LintResult,
    PreconditionResult,
    SubstanceResult,
)


@runtime_checkable
class InvariantHarness(Protocol):
    def check_invariant(
        self,
        spec_slug: str,
        invariant_id: str | None = None,
        run_id: str = "",
        mode: str = "precondition",
    ) -> InvariantResult:
        """Return InvariantResult. Must never expose service-role credential to caller."""
        ...


class NoOpHarness:
    """Default when spec has no write_invariant field."""

    def check_invariant(
        self,
        spec_slug: str,
        invariant_id: str | None = None,
        run_id: str = "",
        mode: str = "precondition",
    ) -> InvariantResult:
        return InvariantResult(ok=True, violation_count=0, reason="no invariant declared")


class SupabaseInvariantHarness:
    """Phase 2 stub: superseded by H1 harness_caller.SupabaseInvariantHarness.

    Kept for backward compatibility. Use scripts.foreman.harness_caller for production.
    """

    def __init__(self, rpc_url: str, anon_key: str) -> None:
        self._rpc_url = rpc_url
        self._anon_key = anon_key

    def check_invariant(
        self,
        spec_slug: str,
        invariant_id: str | None = None,
        run_id: str = "",
        mode: str = "precondition",
    ) -> InvariantResult:
        payload = json.dumps({"spec_slug": spec_slug}).encode()
        req = urllib.request.Request(
            self._rpc_url,
            data=payload,
            headers={
                "apikey": self._anon_key,
                "Authorization": f"Bearer {self._anon_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
        return InvariantResult(
            ok=bool(result.get("ok", False)),
            violation_count=int(result.get("violation_count", 0)),
            reason=str(result.get("reason", "")),
        )


def verify_model_precondition(declared_model: str, api_key: str) -> PreconditionResult:
    """R9.AC3: confirm declared_model is available in Anthropic /v1/models.

    Fails closed on unreachable endpoint.
    Fails if declared model is absent regardless of what IS available (same-family
    substitution and cheaper-same-family substitution are both rejected).
    """
    url = "https://api.anthropic.com/v1/models"
    req = urllib.request.Request(
        url,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read())
    except Exception as exc:
        return PreconditionResult(
            ok=False,
            actual_model="",
            reason=f"model-precondition-unreachable: {exc}",
        )

    available = [m.get("id", "") for m in body.get("data", [])]
    if declared_model in available:
        return PreconditionResult(ok=True, actual_model=declared_model, reason="")

    return PreconditionResult(
        ok=False,
        actual_model=", ".join(available[:3]) + ("..." if len(available) > 3 else ""),
        reason="model-precondition-failed",
    )


def run_antislop_lint_gate(
    diff_text: str,
    spec_demands_tests: bool,
    config: AntislopConfig | None = None,
) -> LintResult:
    """Pre-verify anti-slop static lint gate (anti-slop static lint).

    Thin in-process wrapper around :func:`antislop_lint.run_antislop_lint`; there is
    no credential boundary here, so no subprocess is needed. Fails closed: any
    internal exception returns a FAIL LintResult carrying `error` rather than
    propagating, so a lint bug can never wave a build through unchecked.
    """
    try:
        return run_antislop_lint(diff_text, spec_demands_tests, config)
    except Exception as exc:
        return LintResult(verdict="FAIL", findings=[], error=f"antislop-lint-error: {exc}")


def run_substance_discriminator(
    spec_slug: str,
    run_id: str,
    traceability_path: Path,
    pr_diff_path: Path,
    working_dir: Path,
) -> SubstanceResult:
    """Run the H6 substance discriminator as a subprocess.

    Returns SubstanceResult. Fails closed on subprocess error.
    """
    result = subprocess.run(
        [sys.executable, "scripts/foreman/foreman_substance_discriminator.py",
         "--spec-slug", spec_slug,
         "--run-id", run_id,
         "--traceability", str(traceability_path),
         "--pr-diff", str(pr_diff_path)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=working_dir,
    )
    if result.returncode not in (0, 1) or not result.stdout.strip():
        return SubstanceResult(
            aggregate_verdict="FAIL",
            blocking_criteria=[],
            error="discriminator-subprocess-error",
        )
    data = json.loads(result.stdout)
    return SubstanceResult(**data)
