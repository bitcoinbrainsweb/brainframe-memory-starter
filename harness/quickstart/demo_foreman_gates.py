#!/usr/bin/env python3
"""Quickstart demo: Foreman's deterministic gates, run offline against fixtures.

No accounts, no network, no keys, no builder CLI, no DB. This imports the SHIPPED
gate modules from harness/foreman/scripts/ and runs them against toy fixtures. These
are the real, zero-LLM gates Foreman uses before and after a build:

  - manifest_lint  : is a spec structurally complete enough to dispatch?
  - antislop_lint  : does a diff contain placeholder/slop code or junk files?
  - substance_delta: does a commit actually deliver product, or only docs/scaffold?

Nothing here is reimplemented; the logic is the shipped modules' own.

    python3 harness/quickstart/demo_foreman_gates.py
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))  # harness/quickstart -> repo root
FIXTURES = os.path.join(HERE, "fixtures")
sys.path.insert(0, REPO_ROOT)

from harness.foreman.scripts.manifest_lint import lint_spec  # noqa: E402
from harness.foreman.scripts.antislop_lint import (  # noqa: E402
    format_lint_findings,
    run_antislop_lint,
)
from harness.foreman.scripts.substance_delta import (  # noqa: E402
    ChangedFile,
    evaluate_deliverable_delta,
)


def _load(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read() if name.endswith(".diff") else json.load(f)


def _hr(title):
    print("\n" + "-" * 70)
    print(title)
    print("-" * 70)


def demo_manifest_lint():
    _hr("GATE 1: manifest_lint -- is a spec complete enough to dispatch?")
    for fixture, label in [("toy_spec_good.json", "well-formed spec"),
                           ("toy_spec_incomplete.json", "structurally incomplete spec")]:
        spec = _load(fixture)
        spec.pop("_comment", None)
        result = lint_spec(spec)
        verdict = "PASS (dispatch)" if result.clean else "REFUSE (do not dispatch)"
        print(f"\n  {label}  [{spec['slug']}]  ->  {verdict}")
        if result.clean:
            print(f"    scope boundary resolved: {result.scope}")
        else:
            for v in result.violations:
                print(f"    - {v.field}: {v.message}")


def demo_antislop_lint():
    _hr("GATE 2: antislop_lint -- does the diff contain slop or junk?")
    for fixture, label, demands_tests in [
        ("toy_diff_clean.diff", "clean diff (real code + a test)", True),
        ("toy_diff_sloppy.diff", "sloppy diff (placeholders, stub body)", False),
    ]:
        diff = _load(fixture)
        result = run_antislop_lint(diff, spec_demands_tests=demands_tests)
        print(f"\n  {label}  ->  {result.verdict}")
        if result.findings:
            print(format_lint_findings(result))
        else:
            print("    (no findings)")


def demo_substance_delta():
    _hr("GATE 3: substance_delta -- does the commit deliver product, or empty calories?")
    cases = _load("substance_cases.json")
    for key, label in [("delivering", "commit with a real deliverable"),
                       ("empty_calorie", "commit with only docs + import scaffold")]:
        changed = [
            ChangedFile(path=c["path"], status=c["status"],
                        added_content_lines=c["added_content_lines"])
            for c in cases[key]
        ]
        result = evaluate_deliverable_delta(changed)
        print(f"\n  {label}  ->  {result.verdict}   ({result.reason})")
        print(f"    considered:  {result.considered}")
        print(f"    substantive: {result.substantive or '(none)'}")


def main():
    print("=" * 70)
    print("FOREMAN GATES QUICKSTART -- real deterministic gates, offline")
    print("=" * 70)
    print("Modules under test: harness/foreman/scripts/{manifest_lint, antislop_lint, substance_delta}.py")
    demo_manifest_lint()
    demo_antislop_lint()
    demo_substance_delta()
    print("\n" + "=" * 70)
    print("All three gates are pure and offline: no DB, no builder CLI, no network.")
    print("Real path: harness/foreman/SETUP.md (these gates run inside the build/verify loop).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
