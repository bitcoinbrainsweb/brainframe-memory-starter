#!/usr/bin/env python3
"""Foreman trusted DB-invariant harness.

Spec: trusted DB harness
Invoked only by foreman orchestrator as a subprocess. Never imported.

CLI:
  --invariant-id STR  (required)
  --spec-slug    STR  (required)
  --run-id       STR  (required)
  --mode         {precondition|smoke}  (required)

Stdout (exactly 4 fields, nothing else):
  {"invariant_id": "...", "verdict": "PASS|FAIL", "violation_count": N, "reason_code": "..."}
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

# ── Constants ─────────────────────────────────────────────────────────────────
REGISTRY_PATH = Path(__file__).parent / "invariant_registry.json"

VALID_ARG_NAMES = frozenset({"--invariant-id", "--spec-slug", "--run-id", "--mode"})
VALID_MODES = frozenset({"precondition", "smoke"})

REASON_CODES = frozenset({
    "ok",
    "invariant-not-registered",
    "invariant-ref-mismatch",
    "project-mismatch",
    "untrusted-parameter",
    "caller-not-authorized",
    "role-unconfirmed",
    "db-unreachable",
    "query-error",
    "timeout",
    "mode-violation",
    "write-invariant-rejected",
})

_SQL_INJECTION_RE = re.compile(
    r"[;'\"]|--|\b(SELECT|INSERT|UPDATE|DELETE|DROP|UNION|EXEC|CAST|CONVERT)\b",
    re.IGNORECASE,
)

# ── Redaction ─────────────────────────────────────────────────────────────────
_redact_target: str = ""


def _set_redact_target(key: str) -> None:
    global _redact_target
    _redact_target = key


def redact(s: str) -> str:
    t = _redact_target
    if t and t in str(s):
        return str(s).replace(t, "[REDACTED]")
    return str(s)


# ── Verdict builder ───────────────────────────────────────────────────────────
def _verdict(
    invariant_id: Any,
    verdict: str,
    violation_count: int,
    reason_code: str,
) -> dict:
    assert reason_code in REASON_CODES, f"BUG: invalid reason_code {reason_code!r}"
    return {
        "invariant_id": invariant_id,
        "verdict": verdict,
        "violation_count": violation_count,
        "reason_code": reason_code,
    }


# ── Registry ──────────────────────────────────────────────────────────────────
def content_hash(query_text: str) -> str:
    return hashlib.sha256(query_text.encode()).hexdigest()


def load_registry(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


# ── Credential fetch ──────────────────────────────────────────────────────────
def _default_fetch_creds() -> dict:
    """Return a dict of secrets. By default reads a JSON blob from the environment
    variable SECRETS_JSON; point SECRETS_DOWNLOAD_CMD at your secrets manager's
    "download all as JSON" command to fetch them at run time instead.
    """
    cmd = os.environ.get("SECRETS_DOWNLOAD_CMD")
    if cmd:
        result = subprocess.run(
            shlex.split(cmd),
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            raise RuntimeError("secrets download failed")
        return json.loads(result.stdout)
    return json.loads(os.environ.get("SECRETS_JSON", "{}"))


# ── Audit event ───────────────────────────────────────────────────────────────
def _post_audit_event(
    run_id: str,
    spec_slug: str,
    invariant_id: str,
    verdict: str,
    violation_count: int,
    reason_code: str,
    mode: str,
    service_key: str,
) -> None:
    """POST harness verdict to state_events via REST. Best-effort; never raises."""
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    if not url or not service_key:
        return
    import urllib.request  # noqa: PLC0415
    payload = json.dumps([{
        "event_type": "harness_verdict",
        "entity_type": "invariant",
        "entity_slug": invariant_id,
        "actor": "foreman_db_harness",
        "after": {
            "run_id": run_id,
            "spec_slug": spec_slug,
            "invariant_id": invariant_id,
            "verdict": verdict,
            "violation_count": violation_count,
            "reason_code": reason_code,
            "mode": mode,
        },
    }]).encode()
    req = urllib.request.Request(
        f"{url}/rest/v1/state_events",
        data=payload,
        headers={
            "apikey": service_key,
            "Authorization": f"Bearer {service_key}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as _:
            pass
    except Exception:
        pass


# ── Smoke schema context manager ────────────────────────────────────
def _smoke_schema_name(mode: str, run_id: str) -> str:
    return "smoke_test_" + hashlib.sha256(f"{mode}:{run_id}".encode()).hexdigest()[:12]


@contextmanager
def _smoke_schema_ctx(conn: Any, schema_name: str):
    """Create smoke schema, yield name, drop schema on exit."""
    with conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema_name}"')
    conn.commit()
    try:
        yield schema_name
    finally:
        try:
            with conn.cursor() as cur:
                cur.execute(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE')
            conn.commit()
        except Exception:
            pass


# ── Connection helper ─────────────────────────────────────────────────────────
def _close(conn: Any) -> None:
    try:
        conn.close()
    except Exception:
        pass


# ── Inner query execution ─────────────────────────────────────────────────────
def _do_query(
    invariant_id: str,
    spec_slug: str,
    run_id: str,
    mode: str,
    violation_query: str,
    expected_count: int,
    fetch_creds_fn: Callable[[], dict],
    connect_fn: Callable[[str], Any],
    post_audit_fn: Callable[[dict, str], None] | None = None,
) -> dict:
    """Fetch creds, confirm role, run query. Returns verdict dict; never raises."""

    def _emit(v: dict, key: str = "") -> dict:
        if post_audit_fn is not None:
            try:
                post_audit_fn(v, key)
            except Exception:
                pass
        return v

    # Fetch credentials at call time
    try:
        creds = fetch_creds_fn()
    except Exception:
        return _emit(_verdict(invariant_id, "FAIL", 0, "db-unreachable"))

    service_role_key = creds.get("SUPABASE_SERVICE_KEY", "")
    _set_redact_target(service_role_key)
    db_url = creds.get("SUPABASE_DB_URL", "")

    if not db_url:
        return _emit(_verdict(invariant_id, "FAIL", 0, "mode-violation"), service_role_key)

    # Connect
    try:
        conn = connect_fn(db_url)
    except Exception:
        return _emit(_verdict(invariant_id, "FAIL", 0, "db-unreachable"), service_role_key)

    # R6.AC2: confirm service_role before any query
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT current_role()")
            row = cur.fetchone()
            role = row[0] if row else None
    except Exception:
        _close(conn)
        return _emit(_verdict(invariant_id, "FAIL", 0, "query-error"), service_role_key)

    if role != "service_role":
        _close(conn)
        return _emit(_verdict(invariant_id, "FAIL", 0, "role-unconfirmed"), service_role_key)

    # Run violation query (read-only)
    if mode == "smoke":
        schema_name = _smoke_schema_name(mode, run_id)
        try:
            with _smoke_schema_ctx(conn, schema_name):
                with conn.cursor() as cur:
                    cur.execute(violation_query)
                    row = cur.fetchone()
                    violation_count = int(row[0]) if row else 0
        except Exception:
            _close(conn)
            return _emit(_verdict(invariant_id, "FAIL", 0, "query-error"), service_role_key)
    else:
        try:
            with conn.cursor() as cur:
                cur.execute(violation_query)
                row = cur.fetchone()
                violation_count = int(row[0]) if row else 0
        except Exception:
            _close(conn)
            return _emit(_verdict(invariant_id, "FAIL", 0, "query-error"), service_role_key)

    _close(conn)

    if violation_count == expected_count:
        return _emit(_verdict(invariant_id, "PASS", violation_count, "ok"), service_role_key)
    return _emit(_verdict(invariant_id, "FAIL", violation_count, "ok"), service_role_key)


# ── Harness orchestrator ──────────────────────────────────────────────────────
def run_harness(
    invariant_id: str,
    spec_slug: str,
    run_id: str,
    mode: str,
    *,
    caller_token: str | None = None,
    registry_path: Path = REGISTRY_PATH,
    fetch_creds_fn: Callable[[], dict] | None = None,
    connect_fn: Callable[[str], Any] | None = None,
    post_audit_fn: Callable[[dict, str], None] | None = None,
    timeout_seconds: int | None = None,
) -> dict:
    """Evaluate an invariant and return a verdict dict. Never raises."""
    if fetch_creds_fn is None:
        fetch_creds_fn = _default_fetch_creds
    if connect_fn is None:
        import psycopg2  # noqa: PLC0415
        connect_fn = psycopg2.connect

    # R3.AC3: caller identity
    token = caller_token if caller_token is not None else os.environ.get("FOREMAN_CALLER_TOKEN", "")
    if not token:
        return _verdict(invariant_id, "FAIL", 0, "caller-not-authorized")

    # R1.AC2: load registry
    try:
        registry = load_registry(registry_path)
    except (FileNotFoundError, json.JSONDecodeError):
        return _verdict(invariant_id, "FAIL", 0, "invariant-not-registered")

    entry = registry.get(invariant_id)
    if entry is None:
        return _verdict(invariant_id, "FAIL", 0, "invariant-not-registered")

    # R1.AC3: registry entry must declare a project that matches the caller's spec_slug
    # project-mismatch fires when absent, empty, or does not match spec_slug
    if not entry.get("project") or entry.get("project") != spec_slug:
        return _verdict(invariant_id, "FAIL", 0, "project-mismatch")

    # R1.AC4: content hash integrity
    violation_query = entry.get("violation_query", "")
    if content_hash(violation_query) != entry.get("content_hash", ""):
        return _verdict(invariant_id, "FAIL", 0, "invariant-ref-mismatch")

    # R5.AC4: write invariants unconditionally rejected
    if entry.get("allows_write", False):
        return _verdict(invariant_id, "FAIL", 0, "write-invariant-rejected")

    expected_count = int(entry.get("expected_count", 0))

    # Build audit function with run context
    def _audit(verdict_dict: dict, service_key: str) -> None:
        _post_audit_event(
            run_id=run_id,
            spec_slug=spec_slug,
            invariant_id=invariant_id,
            verdict=verdict_dict["verdict"],
            violation_count=verdict_dict["violation_count"],
            reason_code=verdict_dict["reason_code"],
            mode=mode,
            service_key=service_key,
        )

    effective_audit = post_audit_fn if post_audit_fn is not None else _audit

    # R7: timeout-bounded execution
    timeout_val = (
        timeout_seconds
        if timeout_seconds is not None
        else int(os.environ.get("HARNESS_TIMEOUT_SECONDS", "30"))
    )

    result_box: list = [None]
    error_box: list = [None]

    def _execute() -> None:
        try:
            result_box[0] = _do_query(
                invariant_id=invariant_id,
                spec_slug=spec_slug,
                run_id=run_id,
                mode=mode,
                violation_query=violation_query,
                expected_count=expected_count,
                fetch_creds_fn=fetch_creds_fn,
                connect_fn=connect_fn,
                post_audit_fn=effective_audit,
            )
        except Exception as exc:
            error_box[0] = redact(str(exc))

    worker = threading.Thread(target=_execute, daemon=True)
    worker.start()
    worker.join(timeout=timeout_val)

    if worker.is_alive():
        return _verdict(invariant_id, "FAIL", 0, "timeout")

    if error_box[0] is not None:
        return _verdict(invariant_id, "FAIL", 0, "query-error")

    return result_box[0] or _verdict(invariant_id, "FAIL", 0, "query-error")


# ── Argument parsing ──────────────────────────────────────────────────────────
def parse_args(argv: list) -> tuple[dict, str | None]:
    """Parse CLI args. Returns (parsed_dict, error_reason_code | None)."""
    parsed: dict = {}
    i = 0
    while i < len(argv):
        arg = argv[i]
        if not arg.startswith("--"):
            return {}, "untrusted-parameter"
        if arg not in VALID_ARG_NAMES:
            return {}, "untrusted-parameter"
        if i + 1 >= len(argv):
            return {}, "untrusted-parameter"
        parsed[arg] = argv[i + 1]
        i += 2

    required = {"--invariant-id", "--spec-slug", "--run-id", "--mode"}
    if not required.issubset(parsed.keys()):
        return {}, "untrusted-parameter"

    if parsed["--mode"] not in VALID_MODES:
        return {}, "untrusted-parameter"

    for key in ("--invariant-id", "--spec-slug", "--run-id"):
        if _SQL_INJECTION_RE.search(parsed[key]):
            return {}, "untrusted-parameter"

    return parsed, None


# ── Entry point ───────────────────────────────────────────────────────────────
def main() -> None:
    parsed, err = parse_args(sys.argv[1:])
    if err:
        print(json.dumps(_verdict("?", "FAIL", -1, err)), flush=True)
        sys.exit(1)

    invariant_id = parsed["--invariant-id"]
    result = run_harness(
        invariant_id=invariant_id,
        spec_slug=parsed["--spec-slug"],
        run_id=parsed["--run-id"],
        mode=parsed["--mode"],
    )
    print(json.dumps(result), flush=True)
    sys.exit(0 if result["verdict"] == "PASS" else 1)


if __name__ == "__main__":
    main()
