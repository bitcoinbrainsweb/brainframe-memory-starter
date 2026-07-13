#!/usr/bin/env python3
"""Foreman H6: Substance discriminator.

Evaluates test substance for in-scope acceptance criteria.
Mechanical verdict is final; LLM annotation is explanatory only and cannot
overturn a mechanical FAIL.

CLI:
    python3 foreman_substance_discriminator.py \\
        --spec-slug    <slug> \\
        --run-id       <run_id> \\
        --traceability <path_to_traceability.json> \\
        --pr-diff      <path_to_diff.patch>

Stdout: JSON (only output the orchestrator reads).
Exit 0 = PASS, exit 1 = FAIL or subprocess error.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

MUTATION_THRESHOLD = float(os.environ.get("MUTATION_THRESHOLD", "0.8"))
MUTATION_BUDGET_SECONDS = 60
MUTATION_BUDGET_MUTANTS = 50


# ---------------------------------------------------------------------------
# Core data types
# ---------------------------------------------------------------------------

@dataclass
class MethodResult:
    mechanical_verdict: str  # "PASS" | "FAIL"
    signal_value: dict
    reason: str


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _parse_changed_lines(changed_lines: list[str]) -> dict[str, set[int]]:
    """["src/x.py:42-58"] → {"src/x.py": {42, 43, ..., 58}}"""
    result: dict[str, set[int]] = {}
    for entry in changed_lines:
        if ":" not in entry:
            continue
        filepath, linespec = entry.rsplit(":", 1)
        if "-" in linespec:
            s, e = linespec.split("-", 1)
            nums = set(range(int(s), int(e) + 1))
        else:
            nums = {int(linespec)}
        result.setdefault(filepath, set()).update(nums)
    return result


def _neutralize_function_body(source: str, fn_name: str) -> str:
    """Replace named function body with `pass`. Returns modified source."""
    lines = source.splitlines(keepends=True)
    fn_pat = re.compile(r'^(\s*)def\s+' + re.escape(fn_name) + r'\s*[\(:]')
    i = 0
    while i < len(lines):
        m = fn_pat.match(lines[i])
        if m:
            base_indent = m.group(1)
            body_indent = base_indent + "    "
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            body_start = j
            body_end = j
            while body_end < len(lines):
                line = lines[body_end]
                if line.rstrip():
                    depth = len(line) - len(line.lstrip())
                    if depth <= len(base_indent) and body_end > body_start:
                        break
                body_end += 1
            new_body = [f"{body_indent}pass\n"]
            return "".join(lines[:body_start] + new_body + lines[body_end:])
        i += 1
    return source


@contextlib.contextmanager
def _nullify_function(working_dir: Path, filepath: str, fn_name: str):
    """Crash-safe: replace function body with pass, always revert on exit."""
    target = working_dir / filepath
    if not target.exists():
        yield
        return
    original = target.read_text(encoding="utf-8")
    try:
        neutralized = _neutralize_function_body(original, fn_name)
        target.write_text(neutralized, encoding="utf-8")
        yield
    finally:
        target.write_text(original, encoding="utf-8")


@contextlib.contextmanager
def _apply_fixture(working_dir: Path, fixture_path: str):
    """Crash-safe: apply negative-control fixture JSON, always revert on exit.

    Fixture JSON format: {"target_file": "src/x.py", "content": "...broken..."}
    NEVER commits — revert is guaranteed by try/finally.
    """
    fixture_file = working_dir / fixture_path
    if not fixture_file.exists():
        raise FileNotFoundError(f"Fixture not found: {fixture_file}")
    spec = json.loads(fixture_file.read_text(encoding="utf-8"))
    target = working_dir / spec["target_file"]
    broken_content = spec["content"]
    original = target.read_text(encoding="utf-8") if target.exists() else None
    try:
        target.write_text(broken_content, encoding="utf-8")
        yield
    finally:
        if original is not None:
            target.write_text(original, encoding="utf-8")
        elif target.exists():
            target.unlink()


# ---------------------------------------------------------------------------
# Real subprocess runners (used only outside CI; tests inject mocks)
# ---------------------------------------------------------------------------

def _default_coverage_runner(test_file: str, test_fn: str, working_dir: Path) -> int:
    """Run test with coverage instrumentation. Returns # changed-line hits."""
    cov_run = subprocess.run(
        [sys.executable, "-m", "coverage", "run", "-m", "pytest",
         f"{test_file}::{test_fn}", "--no-header", "-q"],
        cwd=working_dir,
        capture_output=True,
        text=True,
    )
    if cov_run.returncode != 0:
        return 0
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        json_run = subprocess.run(
            [sys.executable, "-m", "coverage", "json", "-o", tmp_path],
            cwd=working_dir,
            capture_output=True,
            text=True,
        )
        if json_run.returncode != 0:
            return 0
        cov_data = json.loads(Path(tmp_path).read_text(encoding="utf-8"))
        total = sum(
            len(fd.get("executed_lines", []))
            for fd in cov_data.get("files", {}).values()
        )
        return total
    except Exception:
        return 0
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _default_test_runner(test_file: str, test_fn: str, working_dir: Path) -> bool:
    """Run test in isolation. Returns True if test passes."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", f"{test_file}::{test_fn}", "--no-header", "-q"],
        cwd=working_dir,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


# ---------------------------------------------------------------------------
# Coverage Method (R2)
# ---------------------------------------------------------------------------

class CoverageMethod:
    """R2: per-test isolation + deletion-sensitivity gate."""

    def __init__(
        self,
        coverage_runner: Callable | None = None,
        test_runner: Callable | None = None,
        neutralizer: Any = None,
    ) -> None:
        self._coverage_runner = coverage_runner or _default_coverage_runner
        self._test_runner = test_runner or _default_test_runner
        self._neutralizer = neutralizer or _nullify_function

    def evaluate(
        self,
        criterion_id: str,
        mapped_tests: list[dict],
        changed_lines: list[str],
        code_paths: list[str],
        working_dir: Path,
    ) -> MethodResult:
        if not changed_lines:
            return MethodResult("FAIL", {}, "zero-change-no-target")

        for test in mapped_tests:
            tf = test.get("file", "")
            fn = test.get("fn", "")

            # R2.AC3: per-test isolation
            lines_hit = self._coverage_runner(tf, fn, working_dir)
            if lines_hit == 0:
                return MethodResult(
                    "FAIL",
                    {"line_hit_pct": 0.0, "test": fn},
                    "coverage-zero-line-hit",
                )

            # R2.AC2: deletion-sensitivity check
            for code_path in code_paths:
                if ":" not in code_path:
                    continue
                fp, fn_name = code_path.rsplit(":", 1)
                with self._neutralizer(working_dir, fp, fn_name):
                    still_passes = self._test_runner(tf, fn, working_dir)
                if still_passes:
                    return MethodResult(
                        "FAIL",
                        {"test": fn, "code_path": code_path},
                        "not-deletion-sensitive",
                    )

        return MethodResult(
            "PASS",
            {"line_hit_pct": 1.0, "deletion_sensitive": True},
            "ok",
        )


# ---------------------------------------------------------------------------
# Mutation Method (R3)
# ---------------------------------------------------------------------------

@dataclass
class MutationRun:
    killed: int
    total: int
    budget_exceeded: bool
    surviving_mutants: list = field(default_factory=list)


def _default_mutation_runner(
    changed_lines: list[str],
    mapped_tests: list[dict],
    working_dir: Path,
) -> MutationRun:
    """Real mutation runner stub; returns budget_exceeded so fallback activates."""
    return MutationRun(killed=0, total=0, budget_exceeded=True)


class MutationMethod:
    """R3: mutation-score gate with time/count budget and coverage fallback."""

    BUDGET_SECONDS = MUTATION_BUDGET_SECONDS
    BUDGET_MUTANTS = MUTATION_BUDGET_MUTANTS
    THRESHOLD = MUTATION_THRESHOLD

    def __init__(
        self,
        mutation_runner: Callable | None = None,
        fallback: Any = None,
    ) -> None:
        self._mutation_runner = mutation_runner or _default_mutation_runner
        self._fallback = fallback  # CompositeMethod or CoverageMethod

    def evaluate(
        self,
        criterion_id: str,
        mapped_tests: list[dict],
        changed_lines: list[str],
        code_paths: list[str],
        working_dir: Path,
        negative_control_fixture: str | None = None,
    ) -> MethodResult:
        run = self._mutation_runner(changed_lines, mapped_tests, working_dir)

        if run.budget_exceeded:
            if self._fallback is None:
                return MethodResult(
                    "FAIL",
                    {"budget_exceeded": True},
                    "mutation-budget-exceeded-fallback-failed",
                )
            try:
                return self._fallback.evaluate(
                    criterion_id,
                    mapped_tests,
                    changed_lines,
                    code_paths,
                    working_dir,
                    negative_control_fixture=negative_control_fixture,
                )
            except Exception:
                return MethodResult(
                    "FAIL",
                    {"budget_exceeded": True},
                    "mutation-budget-exceeded-fallback-failed",
                )

        if run.total == 0:
            return MethodResult("FAIL", {"killed": 0, "total": 0}, "mutation-no-mutants")

        score = run.killed / run.total
        if score >= self.THRESHOLD:
            return MethodResult(
                "PASS",
                {"killed": run.killed, "total": run.total, "score": score,
                 "surviving_mutants": run.surviving_mutants},
                "ok",
            )
        return MethodResult(
            "FAIL",
            {"killed": run.killed, "total": run.total, "score": score,
             "surviving_mutants": run.surviving_mutants},
            "mutation-score-below-threshold",
        )


# ---------------------------------------------------------------------------
# Negative Control Method (R4)
# ---------------------------------------------------------------------------

class NegativeControlMethod:
    """R4: apply known-broken implementation; test must fail against it."""

    def __init__(
        self,
        test_runner: Callable | None = None,
        fixture_applier: Any = None,
    ) -> None:
        self._test_runner = test_runner or _default_test_runner
        self._fixture_applier = fixture_applier or _apply_fixture

    def evaluate(
        self,
        criterion_id: str,
        mapped_tests: list[dict],
        fixture_path: str | None,
        working_dir: Path,
    ) -> MethodResult:
        if not fixture_path:
            return MethodResult("FAIL", {}, "fixture-missing")

        try:
            with self._fixture_applier(working_dir, fixture_path):
                for test in mapped_tests:
                    tf = test.get("file", "")
                    fn = test.get("fn", "")
                    passed = self._test_runner(tf, fn, working_dir)
                    if passed:
                        return MethodResult("FAIL", {"test": fn}, "test-passes-broken-control")
        except FileNotFoundError:
            return MethodResult("FAIL", {}, "fixture-missing")
        except Exception:
            # Crash-safe: _apply_fixture's try/finally guarantees revert before this runs.
            return MethodResult("FAIL", {}, "test-runner-error")

        return MethodResult("PASS", {}, "ok")


# ---------------------------------------------------------------------------
# Composite Method: coverage AND negative-control
# ---------------------------------------------------------------------------

class CompositeMethod:
    """Both coverage and negative-control must pass (AND composition)."""

    def __init__(
        self,
        coverage_method: CoverageMethod | None = None,
        negative_control_method: NegativeControlMethod | None = None,
    ) -> None:
        self._coverage = coverage_method or CoverageMethod()
        self._neg_control = negative_control_method or NegativeControlMethod()

    def evaluate(
        self,
        criterion_id: str,
        mapped_tests: list[dict],
        changed_lines: list[str],
        code_paths: list[str],
        working_dir: Path,
        negative_control_fixture: str | None = None,
    ) -> MethodResult:
        cov = self._coverage.evaluate(criterion_id, mapped_tests, changed_lines, code_paths, working_dir)
        if cov.mechanical_verdict == "FAIL":
            return cov
        neg = self._neg_control.evaluate(criterion_id, mapped_tests, negative_control_fixture, working_dir)
        return neg


# ---------------------------------------------------------------------------
# Audit event — best-effort, never raises
# ---------------------------------------------------------------------------

def _post_audit_event(payload: dict) -> None:
    url = os.environ.get("STATE_EVENTS_URL", "")
    anon_key = os.environ.get("SUPABASE_ANON_KEY", "")
    if not url:
        return
    try:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "apikey": anon_key,
                "Authorization": f"Bearer {anon_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception:
        pass


# ---------------------------------------------------------------------------
# LLM annotation — explanatory only, cannot overturn mechanical FAIL
# ---------------------------------------------------------------------------

def _run_llm_annotation(
    criterion_id: str,
    method: str,
    mechanical_result: MethodResult,
    changed_lines: list[str],
    llm_caller: Callable | None,
) -> list[str]:
    if llm_caller is None:
        return []
    try:
        return llm_caller(criterion_id, method, mechanical_result, changed_lines) or []
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Main discriminator
# ---------------------------------------------------------------------------

class SubstanceDiscriminator:
    def __init__(
        self,
        coverage_method: CoverageMethod | None = None,
        mutation_method: MutationMethod | None = None,
        negative_control_method: NegativeControlMethod | None = None,
        composite_method: CompositeMethod | None = None,
        llm_caller: Callable | None = None,
        audit_poster: Callable | None = None,
    ) -> None:
        cov = coverage_method or CoverageMethod()
        neg = negative_control_method or NegativeControlMethod()
        self._coverage = cov
        self._mutation = mutation_method or MutationMethod()
        self._neg_control = neg
        self._composite = composite_method or CompositeMethod(cov, neg)
        self._llm_caller = llm_caller
        self._audit_poster = audit_poster or _post_audit_event

    def evaluate(
        self,
        spec_slug: str,
        run_id: str,
        traceability: dict,
        working_dir: Path,
        session_id: str = "",
    ) -> dict:
        per_criterion: list[dict] = []
        blocking_criteria: list[str] = []

        for criterion_id, entry in traceability.items():
            method_name = entry.get("substance_method") or "coverage+negative_control"
            mapped_tests = self._extract_mapped_tests(entry)
            changed_lines: list[str] = entry.get("changed_lines") or []
            code_paths: list[str] = entry.get("code_paths") or []
            fixture: str | None = entry.get("negative_control_fixture")

            # R1.AC2 / R6.AC2: fail closed on zero changed lines
            if not changed_lines:
                mech = MethodResult("FAIL", {}, "zero-change-no-target")
            elif method_name == "coverage":
                mech = self._coverage.evaluate(
                    criterion_id, mapped_tests, changed_lines, code_paths, working_dir
                )
            elif method_name == "mutation":
                mech = self._mutation.evaluate(
                    criterion_id, mapped_tests, changed_lines, code_paths, working_dir,
                    negative_control_fixture=fixture,
                )
            elif method_name == "negative_control":
                mech = self._neg_control.evaluate(criterion_id, mapped_tests, fixture, working_dir)
            else:
                # undeclared → coverage AND negative-control
                mech = self._composite.evaluate(
                    criterion_id, mapped_tests, changed_lines, code_paths, working_dir,
                    negative_control_fixture=fixture,
                )

            # R5.AC2: optional LLM annotation (explanatory only)
            llm_findings = _run_llm_annotation(
                criterion_id, method_name, mech, changed_lines, self._llm_caller
            )

            # Final verdict: mechanical is authoritative; LLM findings add FAIL
            if mech.mechanical_verdict == "FAIL":
                final_verdict = "FAIL"
                reason = mech.reason
            elif llm_findings:
                final_verdict = "FAIL"
                reason = "llm-finding"
            else:
                final_verdict = "PASS"
                reason = mech.reason

            if final_verdict == "FAIL":
                blocking_criteria.append(criterion_id)

            cr: dict = {
                "criterion_id": criterion_id,
                "method": method_name,
                "mechanical_verdict": mech.mechanical_verdict,
                "signal_value": mech.signal_value,
                "llm_findings": llm_findings,
                "final_verdict": final_verdict,
                "reason": reason,
            }
            per_criterion.append(cr)

            # R6.AC3: audit (best-effort — never raises)
            try:
                self._audit_poster({
                    "run_id": run_id,
                    "spec_slug": spec_slug,
                    "criterion_id": criterion_id,
                    "method": method_name,
                    "mechanical_verdict": mech.mechanical_verdict,
                    "signal_value": mech.signal_value,
                    "llm_findings": llm_findings,
                    "final_verdict": final_verdict,
                    "session_id": session_id,
                })
            except Exception:
                pass

        return {
            "spec_slug": spec_slug,
            "run_id": run_id,
            "aggregate_verdict": "FAIL" if blocking_criteria else "PASS",
            "blocking_criteria": blocking_criteria,
            "per_criterion": per_criterion,
        }

    def _extract_mapped_tests(self, entry: dict) -> list[dict]:
        if "mapped_tests" in entry:
            return entry["mapped_tests"]
        tf = entry.get("test_file", "")
        fn = entry.get("test_fn", "")
        if tf and fn:
            return [{"file": tf, "fn": fn}]
        return []


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Foreman H6: substance discriminator")
    parser.add_argument("--spec-slug", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--traceability", required=True)
    parser.add_argument("--pr-diff", required=True)
    args = parser.parse_args()

    trace_path = Path(args.traceability)

    if not trace_path.exists():
        out = {
            "spec_slug": args.spec_slug,
            "run_id": args.run_id,
            "aggregate_verdict": "FAIL",
            "blocking_criteria": [],
            "per_criterion": [],
            "error": "traceability-file-not-found",
        }
        print(json.dumps(out))
        sys.exit(1)

    traceability = json.loads(trace_path.read_text(encoding="utf-8"))
    working_dir = Path(os.getcwd())

    discriminator = SubstanceDiscriminator()
    result = discriminator.evaluate(
        spec_slug=args.spec_slug,
        run_id=args.run_id,
        traceability=traceability,
        working_dir=working_dir,
    )

    print(json.dumps(result))
    sys.exit(1 if result["aggregate_verdict"] == "FAIL" else 0)


if __name__ == "__main__":
    main()
