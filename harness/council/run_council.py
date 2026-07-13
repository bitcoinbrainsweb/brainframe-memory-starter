#!/usr/bin/env python3
"""Research council: run N independent critics over a spec, judge, merge, review.

This is a sanitized, self-contained extraction of a working council runner. The
structural lessons it preserves:

  - N independent, COLD critics review the same spec in parallel. They do not see
    each other's output. Independence is the whole point.
  - The critics come from DIFFERENT model families / providers, so a single
    model's blind spot cannot sink the whole review. Seat 1 here is an Anthropic
    model; the rest are any OpenAI-compatible endpoints you configure.
  - Two paths run: a parallel path (all critics at once) and a sequential path
    (a few critics in a chain, patching accepted findings between them). A judge
    synthesizes each path; a merger unifies both; a post-run reviewer audits the
    council itself, blinded to which path produced which finding.
  - Every critic output is validated against a JSON schema (critic.schema.json).
    A malformed output is retried up to a cap, then marked DROPPED. Only DROPPED
    counts as a degraded seat.

The original wired five named providers directly. Here the five identical
OpenAI-compatible callers are collapsed into one `call_openai_compatible`
parameterized by base URL and model; configure your own seats in CRITIC_SEATS.

Provide your own model API keys and (optionally) a Postgres/Supabase project for
the critique_runs telemetry. See SETUP.md and the root .env.example.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

# Optional schema validation. If jsonschema is installed and critic.schema.json is
# present, each critic output is validated; otherwise validation is skipped and the
# run continues (the same graceful-degradation the original used for its optional
# verifier / dedupe / citations modules).
try:
    import jsonschema  # type: ignore
    _SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "critic.schema.json")
    with open(_SCHEMA_PATH, encoding="utf-8") as _f:
        _CRITIC_SCHEMA = json.load(_f)
    _VALIDATE_AVAILABLE = True
except Exception:
    _CRITIC_SCHEMA = None
    _VALIDATE_AVAILABLE = False

DEFAULT_DATE = time.strftime("%Y-%m-%d", time.gmtime())

# ---------------------------------------------------------------------------
# Seat configuration
#
# Each seat is one independent critic. Seat 1 is an Anthropic model; the rest are
# OpenAI-compatible endpoints (OpenAI, or any vendor exposing /chat/completions).
# The ONLY hard rule is diversity: do not fill every seat from the same family, or
# you lose the independence the council exists to provide. Fill in your own models
# and base URLs via the environment (see .env.example).
# ---------------------------------------------------------------------------

CRITIC_SEATS = [
    {"seat": "seat_1", "provider": "anthropic",
     "model_env": "CRITIC_1_MODEL", "key_env": "ANTHROPIC_API_KEY"},
    {"seat": "seat_2", "provider": "openai_compatible",
     "model_env": "CRITIC_2_MODEL", "key_env": "CRITIC_2_API_KEY", "base_url_env": "CRITIC_2_BASE_URL"},
    {"seat": "seat_3", "provider": "openai_compatible",
     "model_env": "CRITIC_3_MODEL", "key_env": "CRITIC_3_API_KEY", "base_url_env": "CRITIC_3_BASE_URL"},
    {"seat": "seat_4", "provider": "openai_compatible",
     "model_env": "CRITIC_4_MODEL", "key_env": "CRITIC_4_API_KEY", "base_url_env": "CRITIC_4_BASE_URL"},
    {"seat": "seat_5", "provider": "openai_compatible",
     "model_env": "CRITIC_5_MODEL", "key_env": "CRITIC_5_API_KEY", "base_url_env": "CRITIC_5_BASE_URL"},
]

# Seats that count toward the degraded-flag threshold. Additive/experimental seats
# can be excluded from this set so they do not trip the "council degraded" alarm.
_BASELINE_CRITIC_SEATS = frozenset({s["seat"] for s in CRITIC_SEATS})

# The judge and merger run on a single strong model, configured separately so it is
# never the same instance a critic used.
JUDGE_PROVIDER = "openai_compatible"

# Regulated-content triggers: when a spec matches one of these, seats you have not
# cleared for regulated data are skipped (GATED) rather than dispatched. Replace
# these with your own compliance patterns.
REGULATED_TRIGGERS = [
    r"\bPII\b", r"\btransaction records\b", r"\bAML\b", r"\bregulated\b",
]

# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------

def load_secrets() -> dict:
    """Load required and optional secrets from the environment.

    Set SECRETS_DOWNLOAD_CMD to your secrets manager's "download all as JSON"
    command to fetch anything missing from the environment at run time.
    """
    required = ["ANTHROPIC_API_KEY"]
    # Optional: telemetry writer (Postgres/Supabase) and per-seat keys.
    optional = ["SUPABASE_URL", "SUPABASE_SERVICE_KEY"]
    for s in CRITIC_SEATS:
        optional.append(s["key_env"])

    secrets: dict = {}
    for key in required + optional:
        v = os.environ.get(key)
        if v:
            secrets[key] = v.strip()

    missing = [k for k in required if k not in secrets]
    if missing:
        cmd = os.environ.get("SECRETS_DOWNLOAD_CMD")
        if not cmd:
            print(f"MISSING_KEYS: {missing}. Set them as env vars or set "
                  "SECRETS_DOWNLOAD_CMD to your secrets manager download command.",
                  file=sys.stderr)
            sys.exit(2)
        import shlex
        import subprocess
        try:
            out = subprocess.run(shlex.split(cmd), capture_output=True, text=True, timeout=15)
            blob = json.loads(out.stdout)
            for k in missing:
                secrets[k] = str(blob[k]).strip()
        except Exception as e:
            print(f"SECRETS_FETCH_FAILED: {e}", file=sys.stderr)
            sys.exit(2)
    return secrets


# ---------------------------------------------------------------------------
# Provider call helpers
# ---------------------------------------------------------------------------

async def call_anthropic(client, model, prompt, key):
    """Call an Anthropic model via the Messages API."""
    body = {
        "model": model, "max_tokens": 4096, "temperature": 0,
        "messages": [{"role": "user", "content": prompt}],
    }
    r = await client.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
        json=body, timeout=240,
    )
    if r.status_code != 200:
        return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:300]}"}
    d = r.json()
    if not d.get("content"):
        return {"ok": False, "error": f"no content: {str(d)[:300]}"}
    text = "".join(b.get("text", "") for b in d["content"]
                   if isinstance(b, dict) and b.get("type") == "text")
    return {"ok": True, "text": text, "usage": d.get("usage", {})}


async def call_openai_compatible(client, base_url, model, prompt, key, max_tokens=4096):
    """Call any OpenAI-compatible /chat/completions endpoint.

    This one function replaces the five near-identical vendor-specific callers in
    the original (OpenAI, and three other chat-completions vendors). They differed
    only by base URL and model string, so they collapse to a single parameterized
    caller. Point each seat's base_url at whichever vendor you use.
    """
    r = await client.post(
        f"{base_url.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens, "temperature": 0,
        },
        timeout=240,
    )
    if r.status_code != 200:
        return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:300]}"}
    d = r.json()
    if not d.get("choices"):
        return {"ok": False, "error": f"no choices: {str(d)[:300]}"}
    choice = d["choices"][0]
    txt = choice["message"].get("content") or ""
    return {"ok": True, "text": txt, "usage": d.get("usage", {}),
            "finish_reason": choice.get("finish_reason")}


async def _dispatch_seat(client, seat_cfg, prompt, secrets):
    """Dispatch one critic seat according to its provider config."""
    key = secrets.get(seat_cfg["key_env"], "")
    model = os.environ.get(seat_cfg["model_env"], "YOUR_CRITIC_MODEL")
    if not key:
        return {"ok": False, "error": f"no API key ({seat_cfg['key_env']})"}
    if seat_cfg["provider"] == "anthropic":
        return await call_anthropic(client, model, prompt, key)
    base_url = os.environ.get(seat_cfg.get("base_url_env", ""), "https://api.openai.com/v1")
    return await call_openai_compatible(client, base_url, model, prompt, key)


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

CRITIC_PROMPT = """You are critic #{N} on a research council reviewing a spec.

Your job: identify findings: gaps, errors, structural problems, missing requirements, ambiguous behavior, blind spots.

Output STRICT FORMAT. Line 1 must be exactly one of:
FINDINGS:
ABSTAINED:

If FINDINGS, list each finding in prose as:
[severity BLOCKER|REAL|SPECULATIVE] [verdict ACCEPT|REJECT|DECIDE]
1 sentence claim. 1-3 sentences rationale. 1-sentence patch direction.
Cite line numbers or section names.

ACCEPT = recommend patching spec
REJECT = recommend rejecting current spec direction on this point
DECIDE = needs human call

If you cannot engage with the spec output:
ABSTAINED: <one-sentence reason>

Do not write any preamble. Do not explain what you are about to do.

After the prose findings, emit a final fenced JSON block conforming to the council
critic schema (see critic.schema.json): a "seat", a "path" (parallel|sequential),
an "outcome", and a "findings" array. Each finding carries severity, verdict,
claim, rationale, and patch_direction. line_start and line_end, when given, are
1-indexed inclusive. If you quote spec text verbatim in a rationale, surround it
with markdown backticks so it can be mechanically verified against the cited slice.

SPEC:
---
{SPEC}
---"""


JUDGE_PROMPT = """You are the {PATH} path judge for a research council. {N} critics reviewed a spec.

SPEC UNDER REVIEW: {SPEC_SLUG}

Do NOT REJECT any finding on the basis of "this is out of scope" inferred from critic prose; the spec slug above is the authoritative anchor for what is in scope. If a critic finding genuinely cites a section that does not exist in {SPEC_SLUG}, mark it DECIDE with rationale "section not present in spec" rather than REJECT.

Synthesize findings:
- Group equivalent findings across critics
- Identify findings that conflict (note conflicts, do not resolve)
- Identify patterns suggesting structural vs surface issues
- Surface high-confidence ACCEPTs (>=3 critics agree on parallel; >=2 on sequential)
- Surface DECIDEs needing human call

Output:
## Unique findings (deduped within path)
Numbered. [source-seats] [severity] [verdict]
  Claim. Rationale. Patch direction.
## Patch plan
APPLY / DECIDE / REJECT
## Disagreements surfaced

CRITIC OUTPUTS:
---
{CRITIC_OUTPUTS}
---"""


REVIEWER_PROMPT = """You are the Post-Run Reviewer. Audit the council, not the spec.
BLINDED: findings tagged path-A / path-B / both without identifying architecture.

## Process score (1-10, one-line rationale each)
- Coverage
- Independence
- Signal-to-noise
- Judge quality per path
- Abstention handling

## Outcome score (1-10)
- Substantive vs cosmetic
- New problems introduced
- Systemic blind spots

## Path-A vs Path-B audit
- Patterns different?
- "Both" findings actually equivalent?
- Blind spots?

## Verdict
SHIP / REVISE / ESCALATE

MERGER OUTPUT (blinded):
---
{MERGER_BLIND}
---"""


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------

_FENCED_JSON_PATTERN = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.DOTALL | re.IGNORECASE)


def parse_critic_findings_json(text: str):
    """Pull the last fenced JSON block from a critic response, or None."""
    matches = _FENCED_JSON_PATTERN.findall(text or "")
    for block in reversed(matches):
        try:
            return json.loads(block)
        except Exception:
            continue
    return None


def validate_critic_output(critic_result: dict) -> dict:
    """Validate a critic's fenced JSON against critic.schema.json. Never raises.

    Adds parsed_findings and a schema_ok flag. Absent schema tooling degrades to
    schema_ok=None (skipped), matching the original's optional-module pattern.
    """
    parsed = parse_critic_findings_json(critic_result.get("text", ""))
    critic_result["parsed_findings"] = parsed
    if parsed is None:
        critic_result["schema_ok"] = False
        critic_result["schema_error"] = "no_fenced_json_block_or_invalid"
        return critic_result
    if not _VALIDATE_AVAILABLE:
        critic_result["schema_ok"] = None
        critic_result["schema_error"] = "jsonschema_or_schema_file_unavailable"
        return critic_result
    try:
        jsonschema.validate(parsed, _CRITIC_SCHEMA)
        critic_result["schema_ok"] = True
        critic_result["schema_error"] = None
    except Exception as e:
        critic_result["schema_ok"] = False
        critic_result["schema_error"] = f"{type(e).__name__}: {str(e)[:200]}"
    return critic_result


# ---------------------------------------------------------------------------
# Pipeline phases
# ---------------------------------------------------------------------------

def detect_regulated(spec_text: str) -> list[str]:
    """Return the regulated triggers a spec matches (empty if none)."""
    return [t for t in REGULATED_TRIGGERS if re.search(t, spec_text, re.IGNORECASE)]


async def _run_seat_with_retry(client, seat_cfg, prompt, secrets, max_retries=3):
    """Dispatch one seat, retrying on malformed (schema-invalid) output up to the
    cap. After the cap, the seat is marked DROPPED. Only DROPPED is degraded."""
    last = None
    for attempt in range(max_retries + 1):
        r = await _dispatch_seat(client, seat_cfg, prompt, secrets)
        r["seat"] = seat_cfg["seat"]
        if not r.get("ok"):
            last = r
            continue
        validate_critic_output(r)
        if r.get("schema_ok") is not False:  # True or None (skipped) both pass
            r["outcome"] = "COMPLETED"
            return r
        last = r  # schema-invalid: retry
    (last or {}).setdefault("ok", False)
    last["outcome"] = "DROPPED"
    return last


async def parallel_phase(spec_text, secrets, gated_seats=frozenset()):
    """Dispatch every non-gated seat at once. Require at least 2 baseline seats to
    complete, else the parallel path has failed."""
    import httpx
    dispatched = [s for s in CRITIC_SEATS if s["seat"] not in gated_seats]
    print(f"\n[parallel] {len(dispatched)} critics dispatched (asyncio.gather, 240s each)...")
    t0 = time.time()
    async with httpx.AsyncClient() as client:
        async def critic(seat_cfg, n):
            ts = time.time()
            prompt = CRITIC_PROMPT.replace("{N}", str(n)).replace("{SPEC}", spec_text)
            r = await _run_seat_with_retry(client, seat_cfg, prompt, secrets)
            r["duration_s"] = round(time.time() - ts, 1)
            return r
        results = await asyncio.gather(
            *[critic(s, i) for i, s in enumerate(dispatched, 1)]
        )
    print(f"  parallel complete in {time.time() - t0:.0f}s")
    for r in results:
        state = "COMPLETED" if r.get("ok") else "DROPPED"
        print(f"    {r['seat']}: {state}  {r.get('duration_s', 0)}s")

    # Gated seats are recorded but not dispatched.
    for s in CRITIC_SEATS:
        if s["seat"] in gated_seats:
            results.append({"seat": s["seat"], "ok": False, "outcome": "GATED",
                            "error": "regulated-data routing gate", "duration_s": 0})

    baseline_completed = sum(
        1 for r in results
        if r.get("seat") in _BASELINE_CRITIC_SEATS and r.get("ok")
    )
    if baseline_completed < 2:
        raise SystemExit(f"PARALLEL_PATH_FAILED: only {baseline_completed} baseline critics completed")
    return {r["seat"]: r for r in results}


def compute_degraded_flag(parallel: dict) -> int:
    """Count DROPPED outcomes among baseline seats. GATED does not count."""
    return sum(
        1 for seat, r in parallel.items()
        if seat in _BASELINE_CRITIC_SEATS
        and not r.get("ok")
        and r.get("outcome", "DROPPED") != "GATED"
    )


async def sequential_phase(spec_text, secrets):
    """Run the first few seats in a chain, folding accepted findings from each
    critic into the context the next critic sees. This is the counterpoint to the
    parallel path: it can catch findings that only surface after an earlier one."""
    import httpx
    chain = [s for s in CRITIC_SEATS[:3]]
    print(f"\n[sequential] {len(chain)} critics in a chain...")
    accumulated = ""
    results = {}
    async with httpx.AsyncClient() as client:
        for i, seat_cfg in enumerate(chain, 1):
            prompt = CRITIC_PROMPT.replace("{N}", str(i)).replace("{SPEC}", spec_text)
            if accumulated:
                prompt += "\n\nPRIOR ACCEPTED FINDINGS (do not repeat, build on them):\n" + accumulated
            r = await _run_seat_with_retry(client, seat_cfg, prompt, secrets)
            r["duration_s"] = 0
            results[seat_cfg["seat"]] = r
            if r.get("ok"):
                accumulated += f"\n{r['text'][:1500]}"
    return results


async def judges_phase(parallel, sequential, secrets, spec_slug):
    """One judge per path, run in parallel. Both must succeed."""
    import httpx

    def assemble(seat_dict):
        parts = []
        for seat, r in seat_dict.items():
            if r.get("ok") and r.get("text"):
                parts.append(f"--- CRITIC: {seat} ---\n{r['text']}")
        return "\n\n".join(parts)

    p_text, s_text = assemble(parallel), assemble(sequential)
    n_p = sum(1 for r in parallel.values() if r.get("ok"))
    n_s = sum(1 for r in sequential.values() if r.get("ok"))
    judge_key = secrets.get("JUDGE_API_KEY") or secrets.get(CRITIC_SEATS[1]["key_env"], "")
    judge_base = os.environ.get("JUDGE_BASE_URL", "https://api.openai.com/v1")
    judge_model = os.environ.get("JUDGE_MODEL", "YOUR_CRITIC_MODEL")

    async with httpx.AsyncClient() as client:
        def jprompt(path, n, body):
            return (JUDGE_PROMPT.replace("{PATH}", path).replace("{N}", str(n))
                    .replace("{SPEC_SLUG}", spec_slug).replace("{CRITIC_OUTPUTS}", body))
        judge_p, judge_s = await asyncio.gather(
            call_openai_compatible(client, judge_base, judge_model, jprompt("parallel", n_p, p_text), judge_key, 24576),
            call_openai_compatible(client, judge_base, judge_model, jprompt("sequential", n_s, s_text), judge_key, 24576),
        )
    if not (judge_p.get("ok") and judge_s.get("ok")):
        raise SystemExit("BOTH_JUDGES_REQUIRED, aborting")
    return judge_p, judge_s


async def reviewer_phase(merger_blind, secrets):
    """Post-run reviewer audits the council process, blinded to path architecture."""
    import httpx
    key = secrets.get("JUDGE_API_KEY") or secrets.get(CRITIC_SEATS[1]["key_env"], "")
    base = os.environ.get("JUDGE_BASE_URL", "https://api.openai.com/v1")
    model = os.environ.get("JUDGE_MODEL", "YOUR_CRITIC_MODEL")
    prompt = REVIEWER_PROMPT.replace("{MERGER_BLIND}", merger_blind)
    async with httpx.AsyncClient() as client:
        return await call_openai_compatible(client, base, model, prompt, key, 8192)


# ---------------------------------------------------------------------------
# Telemetry: critique_runs writer (best-effort; never blocks the run)
# ---------------------------------------------------------------------------

def _sb_request(method, path, secrets, body=None):
    if "SUPABASE_URL" not in secrets or "SUPABASE_SERVICE_KEY" not in secrets:
        return 0, "supabase_keys_unavailable"
    url = secrets["SUPABASE_URL"].rstrip("/") + path
    key = secrets["SUPABASE_SERVICE_KEY"]
    headers = {
        "apikey": key, "Authorization": f"Bearer {key}",
        "Content-Type": "application/json", "Accept": "application/json",
        "Prefer": "return=representation",
    }
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:500]
    except Exception as e:
        return 0, f"network_error: {e}"


def critique_runs_insert(secrets, spec_path, spec_project, spec_sha, data_class,
                         critics_requested, idempotency_key):
    body = {
        "spec_path": spec_path, "spec_project": spec_project, "spec_sha": spec_sha,
        "data_class": data_class, "status": "running",
        "critics_requested": critics_requested, "idempotency_key": idempotency_key,
        "caller": "manual", "started_at": "now()",
    }
    status, resp = _sb_request("POST", "/rest/v1/critique_runs", secrets, body)
    if status in (200, 201):
        try:
            return json.loads(resp)[0]["id"]
        except Exception:
            return None
    print(f"  [critique_runs] INSERT failed: {status} {resp[:200]}", file=sys.stderr)
    return None


def critique_runs_update(secrets, row_id, **fields):
    if not row_id:
        return
    status, resp = _sb_request("PATCH", f"/rest/v1/critique_runs?id=eq.{row_id}", secrets, fields)
    if status not in (200, 204):
        print(f"  [critique_runs] UPDATE failed: {status} {resp[:200]}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    ap = argparse.ArgumentParser(description="Run a research council over a spec.")
    ap.add_argument("spec_path", help="Path to the spec markdown file under review")
    ap.add_argument("--project", default="project_a")
    ap.add_argument("--data-class", default="public", choices=["public", "confidential", "regulated"])
    args = ap.parse_args()

    secrets = load_secrets()
    spec_text = open(args.spec_path, encoding="utf-8").read()
    spec_slug = os.path.splitext(os.path.basename(args.spec_path))[0]

    # Regulated-content gate: skip seats not cleared for regulated data.
    triggers = detect_regulated(spec_text)
    gated = frozenset()
    if triggers or args.data_class == "regulated":
        cleared = set((os.environ.get("REGULATED_CLEARED_SEATS", "")).split(",")) - {""}
        gated = frozenset(s["seat"] for s in CRITIC_SEATS if s["seat"] not in cleared)
        print(f"[gate] regulated content ({triggers}); gating seats: {sorted(gated)}")

    import hashlib
    spec_sha = hashlib.sha256(spec_text.encode()).hexdigest()
    row_id = critique_runs_insert(
        secrets, args.spec_path, args.project, spec_sha, args.data_class,
        [s["seat"] for s in CRITIC_SEATS if s["seat"] not in gated],
        idempotency_key=f"{spec_sha[:16]}-{DEFAULT_DATE}",
    )

    parallel = await parallel_phase(spec_text, secrets, gated_seats=gated)
    sequential = await sequential_phase(spec_text, secrets)
    judge_p, judge_s = await judges_phase(parallel, sequential, secrets, spec_slug)

    # Merge the two path judgments (blinded), then audit the council.
    merger_blind = (
        "## Path-A judgment\n" + judge_p.get("text", "") +
        "\n\n## Path-B judgment\n" + judge_s.get("text", "")
    )
    review = await reviewer_phase(merger_blind, secrets)

    degraded = compute_degraded_flag(parallel)
    critique_runs_update(
        secrets, row_id, status="complete",
        critics_succeeded=[s for s, r in parallel.items() if r.get("ok")],
        critics_failed=[s for s, r in parallel.items() if not r.get("ok")],
        completed_at="now()",
    )

    print("\n==================== COUNCIL RESULT ====================")
    print(f"spec: {spec_slug}   degraded_seats: {degraded}")
    print("\n--- parallel judge ---\n" + judge_p.get("text", "")[:4000])
    print("\n--- sequential judge ---\n" + judge_s.get("text", "")[:4000])
    print("\n--- post-run review ---\n" + review.get("text", "")[:4000])


if __name__ == "__main__":
    asyncio.run(main())
