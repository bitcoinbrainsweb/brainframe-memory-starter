#!/usr/bin/env python3
"""Quickstart demo: the REAL research-council phase flow with mocked model calls.

No accounts, no network, no keys. This imports the shipped harness/council/
run_council.py and runs its actual pipeline -- parallel critics, sequential
critics, two judges, a blinded post-run reviewer -- but swaps the two network call
seams (`call_anthropic`, `call_openai_compatible`) for functions that return canned
responses from fixtures/council_responses.json. run_council.py itself is NOT
modified: we replace the module-level call functions and inject a stub `httpx`
module so the phases run offline.

What is real here: the seat dispatch, the retry/validation wrapper, the fenced-JSON
parsing, the degraded-seat accounting, the parallel/sequential split, the two-judge
gate, and the blinded merge+review. What is mocked: only the model responses.

Persistence: the real runner writes a `critique_runs` telemetry row to
Postgres/Supabase. That is a separate seam; this demo calls the phase functions
directly and prints a summary instead of writing a row (noted at the end).

    python3 harness/quickstart/demo_council.py
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
COUNCIL_DIR = os.path.join(os.path.dirname(HERE), "council")
FIXTURES = os.path.join(HERE, "fixtures", "council_responses.json")

# --- Inject a stub httpx so run_council's phase functions import cleanly ---------
# The phases do `import httpx` and open `httpx.AsyncClient()`; our mocked call seams
# ignore the client, so a no-op async context manager is all that is needed. This
# only affects this demo process.
if "httpx" not in sys.modules:
    _stub = types.ModuleType("httpx")

    class _AsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    _stub.AsyncClient = _AsyncClient
    sys.modules["httpx"] = _stub

sys.path.insert(0, COUNCIL_DIR)
import run_council  # noqa: E402  (imported after sys.path/httpx setup, by design)

with open(FIXTURES, encoding="utf-8") as f:
    _CANNED = json.load(f)


def _critic_text(prompt: str) -> str:
    """Pick the canned critic response by the critic number in the prompt."""
    m = re.search(r"critic #(\d+)", prompt)
    n = m.group(1) if m else "1"
    return "\n".join(_CANNED["critics"].get(n, _CANNED["critics"]["1"]))


async def mock_anthropic(client, model, prompt, key):
    """Stands in for run_council.call_anthropic (seat 1)."""
    return {"ok": True, "text": _critic_text(prompt), "usage": {}}


async def mock_openai_compatible(client, base_url, model, prompt, key, max_tokens=4096):
    """Stands in for run_council.call_openai_compatible (seats 2-5, judges, reviewer)."""
    if "Post-Run Reviewer" in prompt:
        return {"ok": True, "text": "\n".join(_CANNED["reviewer"]), "usage": {}}
    if "path judge" in prompt:
        key_ = "judge_parallel" if "the parallel path judge" in prompt else "judge_sequential"
        return {"ok": True, "text": "\n".join(_CANNED[key_]), "usage": {}}
    return {"ok": True, "text": _critic_text(prompt), "usage": {}}


# Wire the mocks through the existing call seams (no edit to run_council.py).
run_council.call_anthropic = mock_anthropic
run_council.call_openai_compatible = mock_openai_compatible

# Toy, obviously-synthetic spec under review.
SPEC = """# Acme Widget API v1

## Endpoints
- GET /widgets            list all widgets
- GET /widgets/{id}       fetch one widget
- POST /widgets           create a widget
- PATCH /widgets/{id}     update a widget
- DELETE /widgets/{id}    hard-delete a widget

## Responses
Success bodies return the widget as JSON.

## Non-functional
Should be fast.
"""


def _severity_tally(seat_results: dict) -> dict:
    tally = {"BLOCKER": 0, "REAL": 0, "SPECULATIVE": 0}
    for r in seat_results.values():
        parsed = r.get("parsed_findings") or {}
        for finding in parsed.get("findings", []):
            sev = finding.get("severity")
            if sev in tally:
                tally[sev] += 1
    return tally


async def run() -> int:
    print("=" * 70)
    print("COUNCIL QUICKSTART -- real phase flow, mocked model responses")
    print("=" * 70)
    print(f"jsonschema installed: {run_council._VALIDATE_AVAILABLE} "
          f"(when False, fenced JSON is still parsed; only jsonschema.validate is skipped)")

    secrets = {"ANTHROPIC_API_KEY": "demo-key", "JUDGE_API_KEY": "demo-key"}
    for seat in run_council.CRITIC_SEATS:
        secrets[seat["key_env"]] = "demo-key"

    # Phase 1: parallel critics (all at once, independent, cold).
    parallel = await run_council.parallel_phase(SPEC, secrets)

    # Phase 2: sequential critics (a chain that folds in accepted findings).
    sequential = await run_council.sequential_phase(SPEC, secrets)
    print("\n[sequential] seat outcomes: " +
          ", ".join(f"{s}={'COMPLETED' if r.get('ok') else 'DROPPED'}"
                    for s, r in sequential.items()))

    # Phase 3: one judge per path (both required).
    judge_p, judge_s = await run_council.judges_phase(parallel, sequential, secrets, "acme-widget-api-v1")

    # Phase 4: blinded merge + post-run review.
    merger_blind = ("## Path-A judgment\n" + judge_p.get("text", "") +
                    "\n\n## Path-B judgment\n" + judge_s.get("text", ""))
    review = await run_council.reviewer_phase(merger_blind, secrets)

    degraded = run_council.compute_degraded_flag(parallel)
    tally = _severity_tally(parallel)

    print("\n" + "=" * 70)
    print("COUNCIL RESULT")
    print("=" * 70)
    print(f"critics: {len(parallel)}   degraded_seats: {degraded}   "
          f"findings by severity: {tally}")
    print("\n--- parallel judge ---\n" + judge_p.get("text", ""))
    print("\n--- sequential judge ---\n" + judge_s.get("text", ""))
    print("\n--- post-run review (blinded) ---\n" + review.get("text", ""))

    print("\nPersistence: the real runner writes a critique_runs row to "
          "Postgres/Supabase here; skipped in the quickstart (no DB).")
    print("Real path: harness/council/SETUP.md (your model API keys per seat).")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
