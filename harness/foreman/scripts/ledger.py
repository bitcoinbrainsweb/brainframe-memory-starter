"""Ledger backend for Phase 1.

write-ahead semantics -- every transition written before the action begins.
state_events row emitted per transition, attributed to session.

LedgerBackend: protocol (interface).
InMemoryLedger: for tests (no network calls).
SupabaseLedger: real Supabase REST API implementation.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol, runtime_checkable

from .agent_harness import _redact
from .models import ForemanIntegrityError


def _redact_detail(detail: dict) -> dict:
    """Recursively redact credential-like strings in all string values."""
    result: dict = {}
    for k, v in detail.items():
        if isinstance(v, str):
            result[k] = _redact(v)
        elif isinstance(v, dict):
            result[k] = _redact_detail(v)
        elif isinstance(v, list):
            result[k] = [_redact(i) if isinstance(i, str) else i for i in v]
        else:
            result[k] = v
    return result


@dataclass
class TransitionRecord:
    spec_slug: str
    new_status: str
    prior_status: str
    fields: dict
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@runtime_checkable
class LedgerBackend(Protocol):
    """Interface for all ledger operations.

    All write methods must complete before the corresponding action begins.
    """

    def create_run(
        self,
        run_id: str,
        spec_slugs: list[str],
        session_id: str,
    ) -> dict:
        """Insert build_runs row. Returns row dict with 'id' (UUID) and 'run_id' (text)."""
        ...

    def create_spec_row(
        self,
        run_uuid: str,
        run_id: str,
        spec: dict,
        position: int = 0,
    ) -> dict:
        """Insert build_run_specs row at status=queued. Returns row dict."""
        ...

    def transition(
        self,
        run_uuid: str,
        run_id: str,
        spec_slug: str,
        new_status: str,
        prior_status: str,
        *,
        branch_name: str | None = None,
        base_sha: str | None = None,
        commit_sha: str | None = None,
        attempt: int | None = None,
        builder_model: str | None = None,
        verifier_model: str | None = None,
        verify_result: str | None = None,
        verifier_findings: dict | None = None,
        park_reason: str | None = None,
        session_id: str | None = None,
    ) -> None:
        """Write-ahead transition. Called before the action described by new_status."""
        ...

    def update_data(
        self,
        run_uuid: str,
        spec_slug: str,
        fields: dict,
    ) -> None:
        """Patch data columns without changing status (no state_event emitted)."""
        ...

    def update_run_status(
        self,
        run_uuid: str,
        new_status: str,
        report: dict | None = None,
    ) -> None:
        """Update build_runs.status."""
        ...

    def fetch_spec(
        self,
        slug: str,
    ) -> dict | None:
        """Fetch a spec row from the specs table. Returns None if not found."""
        ...

    def fetch_spec_body(self, comms_path: str) -> str | None:
        """Fetch raw markdown body for a spec from your spec repo via the authenticated GitHub API.
        Returns None on failure (caller must degrade gracefully).
        comms_path is the relative path within the repo, e.g. 'specs/project_a/my-spec.md'.
        """
        ...

    # ------------------------------------------------------------------
    # Phase 2: foreman_tasks layer
    # ------------------------------------------------------------------

    def create_task_row(
        self,
        run_id: str,
        spec_slug: str,
        build_order: int,
        depends_on: list[str],
        independent: bool,
        session_id: str,
    ) -> dict:
        """INSERT foreman_tasks row at status=queued."""
        ...

    def update_task_status(
        self,
        run_id: str,
        spec_slug: str,
        new_status: str,
        **fields: Any,
    ) -> None:
        """UPDATE foreman_tasks + INSERT new build_run_specs row (append-only)."""
        ...

    def claim_task(
        self,
        run_id: str,
        spec_slug: str,
        session_id: str,
    ) -> bool:
        """Atomically claim task via claim_foreman_task RPC. Returns True if claim succeeded.
        Also writes a build_run_specs row at status=building.
        """
        ...

    def list_resumable_tasks(self, run_id: str) -> list[dict]:
        """SELECT foreman_tasks WHERE run_id=$run_id AND status NOT IN terminal statuses."""
        ...

    def emit_run_event(
        self,
        run_id: str,
        event: str,
        *,
        task_id: str | None = None,
        spec_slug: str | None = None,
        detail: dict | None = None,
    ) -> None:
        """Append a foreman_run_events row. Must not raise into the caller."""
        ...

    def query_non_terminal_tasks(self, spec_slugs: list[str]) -> list[dict]:
        """Return all foreman_tasks rows for the given spec_slugs where status is
        in ('queued','building','verifying','merging'). Used by the single-flight guard."""
        ...

    def recent_dispositions(self, run_id: str, spec_slug: str, limit: int) -> list[str]:
        """Return the last `limit` terminal park_reasons for spec_slug in run_id,
        most recent first. Used by the R4 circuit breaker."""
        ...

    def patch_heartbeat(self, run_id: str, spec_slug: str, timestamp: str) -> None:
        """Write last_heartbeat_at without changing status. Must not raise."""
        ...

    def fetch_prior_verify_findings(self, spec_slug: str, current_run_id: str) -> str | None:
        """return most recent verify findings from a prior run parked
        verify-failed-retry or spec-wallclock-ceiling-exceeded for this spec.
        Returns None if no such prior run exists."""
        ...


class InMemoryLedger:
    """Test ledger: records all writes in memory, never touches the network.

    Ledger writes in tests go here, never into live DB tables.
    """

    def __init__(self) -> None:
        self._transitions: list[TransitionRecord] = []
        self._data_patches: list[dict] = []
        self._run_rows: list[dict] = []
        self._spec_rows: list[dict] = []
        self._run_status_updates: list[dict] = []
        self._specs: dict[str, dict] = {}  # slug -> row; seeded by tests
        self._spec_bodies: dict[str, str] = {}  # comms_path -> body; seeded by tests

        # Phase 2: foreman_tasks layer
        self._task_rows: dict[tuple[str, str], dict] = {}  # (run_id, spec_slug) -> current row
        self._build_run_specs_inserts: list[dict] = []  # append-only Phase 2 build_run_specs rows
        # Merged (upserted) current build_run_specs row per (text_run_id, spec_slug).
        # Mirrors the live table's ON CONFLICT (run_id, spec_slug) DO UPDATE: fields
        # persist across transitions so verify_result='PASS' survives to 'committed'.
        self._brs_current: dict[tuple[str, str], dict] = {}
        self._claim_calls: list[dict] = []  # track all claim_task invocations
        self._write_log: list[dict] = []  # ordered log of all writes (for write-ahead tests)
        self._claim_lock: threading.Lock = threading.Lock()

        # R3: run event trail
        self._run_events: list[dict] = []

        # Pre-dispatch manifest-lint pass records (admin-foreman-predispatch-manifest-lint-v1)
        self._manifest_lint_records: list[dict] = []

    def record_manifest_lint(
        self, session_id: str, spec_slugs: list[str], bundle_hash: str
    ) -> None:
        """Record a clean-bundle manifest-lint pass with its bundle hash."""
        self._manifest_lint_records.append({
            "session_id": session_id,
            "spec_slugs": list(spec_slugs),
            "bundle_hash": bundle_hash,
        })

    def manifest_lint_records(self) -> list[dict]:
        return [dict(r) for r in self._manifest_lint_records]

    def seed_spec(self, spec_row: dict) -> None:
        """Seed a spec row for fetch_spec to return."""
        self._specs[spec_row["slug"]] = spec_row

    def fetch_spec(self, slug: str) -> dict | None:
        return self._specs.get(slug)

    def seed_spec_body(self, comms_path: str, body: str) -> None:
        self._spec_bodies[comms_path] = body

    def fetch_spec_body(self, comms_path: str) -> str | None:
        return self._spec_bodies.get(comms_path)

    def create_run(self, run_id: str, spec_slugs: list[str], session_id: str) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        row = {
            "id": str(uuid.uuid4()),
            "run_id": run_id,
            "project": "project_a",
            "status": "running",
            "bundle": json.dumps(spec_slugs),
            "ordered_bundle": json.dumps(spec_slugs),
            "session_id": session_id,
            "started_at": now,
            "created_at": now,
            "completed_at": None,
        }
        self._run_rows.append(row)
        return row

    def list_stale_running_runs(self, older_than_hours: int = 24) -> list[dict]:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=older_than_hours)
        out: list[dict] = []
        for r in self._run_rows:
            if r.get("status") != "running":
                continue
            ts = r.get("started_at") or r.get("created_at")
            if not ts:
                out.append(dict(r))
                continue
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
            except (ValueError, AttributeError):
                out.append(dict(r))
                continue
            if dt < cutoff:
                out.append(dict(r))
        return out

    def list_running_runs(self) -> list[dict]:
        """All build_runs currently in 'running', regardless of age. Used by the
        kill reconciler, which decides liveness from task heartbeats not run age."""
        return [dict(r) for r in self._run_rows if r.get("status") == "running"]

    def create_spec_row(
        self,
        run_uuid: str,
        run_id: str,
        spec: dict,
        position: int = 0,
    ) -> dict:
        slug = spec["slug"]
        ik = f"{run_id}:{slug}:0"
        row = {
            "id": str(uuid.uuid4()),
            "run_id": run_uuid,
            "spec_slug": slug,
            "position": position,
            "build_order": position,
            "attempt": 0,
            "status": "queued",
            "spec_idempotency_key": ik,
        }
        self._spec_rows.append(row)
        return row

    def transition(
        self,
        run_uuid: str,
        run_id: str,
        spec_slug: str,
        new_status: str,
        prior_status: str,
        *,
        branch_name: str | None = None,
        base_sha: str | None = None,
        commit_sha: str | None = None,
        attempt: int | None = None,
        builder_model: str | None = None,
        verifier_model: str | None = None,
        verify_result: str | None = None,
        verifier_findings: dict | None = None,
        park_reason: str | None = None,
        session_id: str | None = None,
    ) -> None:
        rec = TransitionRecord(
            spec_slug=spec_slug,
            new_status=new_status,
            prior_status=prior_status,
            fields={
                "branch_name": branch_name,
                "base_sha": base_sha,
                "commit_sha": commit_sha,
                "attempt": attempt,
                "builder_model": builder_model,
                "verifier_model": verifier_model,
                "verify_result": verify_result,
                "verifier_findings": verifier_findings,
                "park_reason": park_reason,
            },
        )
        self._transitions.append(rec)

    def update_data(self, run_uuid: str, spec_slug: str, fields: dict) -> None:
        self._data_patches.append({"run_uuid": run_uuid, "spec_slug": spec_slug, **fields})

    def update_run_status(self, run_uuid: str, new_status: str, report: dict | None = None) -> None:
        self._run_status_updates.append({"run_uuid": run_uuid, "status": new_status, "report": report})
        for r in self._run_rows:
            if r.get("id") == run_uuid:
                r["status"] = new_status
                if new_status in ("completed", "failed", "cancelled"):
                    r["completed_at"] = datetime.now(timezone.utc).isoformat()
                break

    # ------------------------------------------------------------------
    # Phase 2: foreman_tasks layer
    # ------------------------------------------------------------------

    def create_task_row(
        self,
        run_id: str,
        spec_slug: str,
        build_order: int,
        depends_on: list[str],
        independent: bool,
        session_id: str,
    ) -> dict:
        row: dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "run_id": run_id,
            "spec_slug": spec_slug,
            "build_order": build_order,
            "depends_on": list(depends_on),
            "independent": independent,
            "session_id": session_id,
            "status": "queued",
            "attempt_no": 0,
            "claimed_by": None,
            "claimed_at": None,
            "commit_sha": None,
            "park_reason": None,
            "failure_trail": None,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self._task_rows[(run_id, spec_slug)] = row
        self._write_log.append({"op": "create_task", "run_id": run_id, "spec_slug": spec_slug, "status": "queued"})
        return dict(row)

    def has_pass_ledger_row(self, run_id: str, spec_slug: str) -> bool:
        """Does a build_run_specs row with verify_result='PASS' exist for
        this task? Gate for recover_stale's promote-to-committed path."""
        return (self._brs_current.get((run_id, spec_slug)) or {}).get("verify_result") == "PASS"

    def _resolve_run_uuid(self, text_run_id: str) -> str | None:
        for r in self._run_rows:
            if r.get("run_id") == text_run_id:
                return r.get("id")
        return None

    def _upsert_brs(self, run_id: str, spec_slug: str, new_status: str, fields: dict) -> dict:
        """Merge fields into the current build_run_specs row (COALESCE semantics).

        Returns the merged row. Mirrors foreman_transition_task's upsert so
        verify_result='PASS' set at 'merging' survives the 'committed' transition.
        """
        key = (run_id, spec_slug)
        cur = self._brs_current.get(key)
        if cur is None:
            cur = {
                "run_id": self._resolve_run_uuid(run_id),
                "text_run_id": run_id,
                "spec_slug": spec_slug,
            }
            self._brs_current[key] = cur
        cur["status"] = new_status
        for k, v in fields.items():
            if v is not None:
                cur[k] = v
        return cur

    def update_task_status(
        self,
        run_id: str,
        spec_slug: str,
        new_status: str,
        **fields: Any,
    ) -> None:
        # A task may not reach 'committed' without a ledger row carrying
        # verify_result='PASS'. Enforced here (in-process) and by the DB trigger.
        if new_status == "committed":
            merged = self._brs_current.get((run_id, spec_slug)) or {}
            incoming_pass = fields.get("verify_result") == "PASS"
            if merged.get("verify_result") != "PASS" and not incoming_pass:
                raise ForemanIntegrityError(
                    f"task {run_id}/{spec_slug} cannot reach committed without a "
                    f"build_run_specs verify_result=PASS row"
                )
        key = (run_id, spec_slug)
        row = self._task_rows[key]
        row["status"] = new_status
        row["updated_at"] = datetime.now(timezone.utc).isoformat()
        for k, v in fields.items():
            row[k] = v
        # Write-ahead upsert of the merged build_run_specs row, plus the
        # append-only insert log kept for resume tests.
        self._upsert_brs(run_id, spec_slug, new_status, fields)
        brs_row = {"run_id": run_id, "text_run_id": run_id, "spec_slug": spec_slug,
                   "status": new_status, **fields}
        self._build_run_specs_inserts.append(brs_row)
        self._write_log.append({"op": "update_task", "run_id": run_id, "spec_slug": spec_slug, "status": new_status})

    def claim_task(
        self,
        run_id: str,
        spec_slug: str,
        session_id: str,
    ) -> bool:
        with self._claim_lock:
            key = (run_id, spec_slug)
            row = self._task_rows.get(key)
            if row is None or row["status"] != "queued" or row["claimed_by"] is not None:
                return False
            now = datetime.now(timezone.utc).isoformat()
            row["status"] = "building"
            row["claimed_by"] = session_id
            row["claimed_at"] = now
            row["updated_at"] = now
            # Write-ahead build_run_specs 'building' row + merged upsert.
            self._upsert_brs(run_id, spec_slug, "building", {"claimed_by": session_id})
            self._build_run_specs_inserts.append(
                {"run_id": run_id, "text_run_id": run_id, "spec_slug": spec_slug,
                 "status": "building", "claimed_by": session_id}
            )
            self._claim_calls.append({"run_id": run_id, "spec_slug": spec_slug, "session_id": session_id})
            self._write_log.append({"op": "claim", "run_id": run_id, "spec_slug": spec_slug, "status": "building"})
            return True

    def list_resumable_tasks(self, run_id: str) -> list[dict]:
        terminal = {"committed", "parked", "dependent-halted"}
        return [
            dict(row)
            for (rid, _), row in self._task_rows.items()
            if rid == run_id and row["status"] not in terminal
        ]

    def list_run_tasks(self, run_id: str) -> list[dict]:
        """All foreman_tasks rows for a run (any status). Used by the kill reconciler."""
        return [
            dict(row)
            for (rid, _), row in self._task_rows.items()
            if rid == run_id
        ]

    def query_non_terminal_tasks(self, spec_slugs: list[str]) -> list[dict]:
        non_terminal = {"queued", "building", "verifying", "merging"}
        slug_set = set(spec_slugs)
        return [
            dict(row)
            for (_, slug), row in self._task_rows.items()
            if slug in slug_set and row["status"] in non_terminal
        ]

    @property
    def transitions(self) -> list[TransitionRecord]:
        return list(self._transitions)

    @property
    def transition_statuses(self) -> list[str]:
        return [t.new_status for t in self._transitions]

    @property
    def data_patches(self) -> list[dict]:
        return list(self._data_patches)

    def emit_run_event(
        self,
        run_id: str,
        event: str,
        *,
        task_id: str | None = None,
        spec_slug: str | None = None,
        detail: dict | None = None,
    ) -> None:
        self._run_events.append({
            "run_id": run_id,
            "task_id": task_id,
            "spec_slug": spec_slug,
            "event": event,
            "detail": detail,
        })

    def patch_heartbeat(self, run_id: str, spec_slug: str, timestamp: str) -> None:
        """Write last_heartbeat_at without changing task status."""
        key = (run_id, spec_slug)
        row = self._task_rows.get(key)
        if row is not None:
            row["last_heartbeat_at"] = timestamp

    def fetch_run(self, run_id: str) -> dict | None:
        """Fetch a build_runs row by text run_id."""
        for r in self._run_rows:
            if r.get("run_id") == run_id:
                return dict(r)
        return None

    def update_run_bundle(self, run_uuid: str, bundle: list[str], ordered_bundle: list[str]) -> None:
        """Update build_runs.bundle/ordered_bundle for an append."""
        for r in self._run_rows:
            if r.get("id") == run_uuid:
                r["bundle"] = json.dumps(bundle)
                r["ordered_bundle"] = json.dumps(ordered_bundle)
                break

    def fetch_run_events(self, run_id: str) -> list[dict]:
        return [dict(e) for e in self._run_events if e["run_id"] == run_id]

    def recent_dispositions(self, run_id: str, spec_slug: str, limit: int) -> list[str]:
        reasons = [
            (e.get("detail") or {}).get("park_reason")
            for e in self._run_events
            if e["run_id"] == run_id
            and e.get("spec_slug") == spec_slug
            and e.get("event") == "parked"
            and (e.get("detail") or {}).get("park_reason")
        ]
        return list(reversed(reasons))[:limit]

    def fetch_prior_verify_findings(self, spec_slug: str, current_run_id: str) -> str | None:
        """scan stored events from prior runs (not current_run_id) for verify findings
        on verify-failed-retry or spec-wallclock-ceiling-exceeded parks."""
        _retryable = {"verify-failed-retry", "spec-wallclock-ceiling-exceeded"}
        # Collect (run_id, findings) pairs from prior parked events for this spec
        prior: list[tuple[str, str]] = []
        for e in self._run_events:
            if e.get("run_id") == current_run_id:
                continue
            if e.get("spec_slug") != spec_slug:
                continue
            detail = e.get("detail") or {}
            if e.get("event") == "parked" and detail.get("park_reason") in _retryable:
                # Look through failure_trail for any attempt's findings
                ft = detail.get("failure_trail") or {}
                for v in ft.values():
                    if isinstance(v, dict) and v.get("findings"):
                        prior.append((e["run_id"], v["findings"]))
                        break
            elif e.get("event") == "verify_result":
                verdict = detail.get("verdict")
                findings = detail.get("findings")
                if verdict in ("FAIL",) and findings:
                    prior.append((e["run_id"], findings))
        if not prior:
            return None
        # Return findings from the most recently appended prior event
        return prior[-1][1]


# ---------------------------------------------------------------------------
# Supabase REST helpers
# ---------------------------------------------------------------------------

_UA = "foreman/1.0"


def _sb_headers(key: str) -> dict:
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "User-Agent": _UA,
    }


def _sb_get(url: str, key: str, table: str, params: str) -> list:
    full = f"{url}/rest/v1/{table}?{params}"
    h = {**_sb_headers(key)}
    h.pop("Content-Type", None)
    req = urllib.request.Request(full, headers=h)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def _sb_post(url: str, key: str, table: str, data: list | dict) -> list:
    full = f"{url}/rest/v1/{table}"
    h = {**_sb_headers(key), "Prefer": "return=representation"}
    payload = data if isinstance(data, list) else [data]
    req = urllib.request.Request(
        full, data=json.dumps(payload).encode(), headers=h, method="POST"
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def _sb_patch(url: str, key: str, table: str, filter_qs: str, data: dict) -> list:
    full = f"{url}/rest/v1/{table}?{filter_qs}"
    h = {**_sb_headers(key), "Prefer": "return=representation"}
    req = urllib.request.Request(
        full, data=json.dumps(data).encode(), headers=h, method="PATCH"
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def _sb_rpc(url: str, key: str, fn: str, args: dict) -> Any:
    """POST to a Postgres function via PostgREST rpc/. Returns decoded JSON."""
    full = f"{url}/rest/v1/rpc/{fn}"
    req = urllib.request.Request(
        full, data=json.dumps(args).encode(), headers=_sb_headers(key), method="POST"
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = resp.read()
    return json.loads(body) if body else None


class SupabaseLedger:
    """Real Supabase ledger. Credentials supplied by the caller from the environment.

    Never hardcode or print credentials.
    """

    def __init__(self, supabase_url: str, service_key: str) -> None:
        if not supabase_url or not service_key:
            raise ValueError("supabase_url and service_key are required")
        self._url = supabase_url.rstrip("/")
        self._key = service_key

    def fetch_spec(self, slug: str) -> dict | None:
        rows = _sb_get(
            self._url, self._key, "specs",
            f"slug=eq.{urllib.parse.quote(slug)}&select=*&order=created_at.desc&limit=1",
        )
        return rows[0] if rows else None

    def fetch_spec_body(self, comms_path: str) -> str | None:
        pat = os.environ.get("FOREMAN_SPEC_REPO_PAT", "")
        if not pat:
            print("[WARN] FOREMAN_SPEC_REPO_PAT not set; cannot fetch spec body", file=sys.stderr)
            return None
        try:
            repo = os.environ.get("FOREMAN_SPEC_REPO", "YOUR_ORG/YOUR_SPEC_REPO")
            url = f"https://api.github.com/repos/{repo}/contents/{comms_path}"
            req = urllib.request.Request(url, headers={
                "Authorization": f"token {pat}",
                "Accept": "application/vnd.github.v3+json",
                "User-Agent": _UA,
            })
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
            return base64.b64decode(data["content"].replace("\n", "")).decode()
        except Exception as exc:
            print(f"[WARN] fetch_spec_body failed for {comms_path}: {exc}", file=sys.stderr)
            return None

    def create_run(self, run_id: str, spec_slugs: list[str], session_id: str) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        payload = {
            "run_id": run_id,
            "project": "project_a",
            "status": "running",
            "bundle": json.dumps(spec_slugs),
            "ordered_bundle": json.dumps(spec_slugs),
            "session_id": session_id,
            "started_at": now,
        }
        rows = _sb_post(self._url, self._key, "build_runs", [payload])
        return rows[0]

    def create_spec_row(
        self,
        run_uuid: str,
        run_id: str,
        spec: dict,
        position: int = 0,
    ) -> dict:
        slug = spec["slug"]
        ik = f"{run_id}:{slug}:0"
        payload = {
            "run_id": run_uuid,
            "spec_slug": slug,
            "position": position,
            "build_order": position,
            "attempt": 0,
            "retry_count": 0,
            "status": "queued",
            "spec_idempotency_key": ik,
        }
        rows = _sb_post(self._url, self._key, "build_run_specs", [payload])
        return rows[0]

    def transition(
        self,
        run_uuid: str,
        run_id: str,
        spec_slug: str,
        new_status: str,
        prior_status: str,
        *,
        branch_name: str | None = None,
        base_sha: str | None = None,
        commit_sha: str | None = None,
        attempt: int | None = None,
        builder_model: str | None = None,
        verifier_model: str | None = None,
        verify_result: str | None = None,
        verifier_findings: dict | None = None,
        park_reason: str | None = None,
        session_id: str | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        patch: dict[str, Any] = {"status": new_status, "updated_at": now, "heartbeat_at": now}

        if branch_name is not None:
            patch["branch_name"] = branch_name
            patch["build_branch"] = branch_name  # legacy compat
        if base_sha is not None:
            patch["base_sha"] = base_sha
        if commit_sha is not None:
            patch["commit_sha"] = commit_sha
            patch["build_commit_sha"] = commit_sha  # legacy compat
        if attempt is not None:
            patch["attempt"] = attempt
            patch["retry_count"] = attempt
            patch["spec_idempotency_key"] = f"{run_id}:{spec_slug}:{attempt}"
        if builder_model is not None:
            patch["builder_model"] = builder_model
        if verifier_model is not None:
            patch["verifier_model"] = verifier_model
        if verify_result is not None:
            patch["verify_result"] = verify_result
        if verifier_findings is not None:
            patch["verifier_findings"] = json.dumps(verifier_findings)
            patch["verify_report"] = json.dumps(verifier_findings)  # legacy compat
        if park_reason is not None:
            patch["park_reason"] = park_reason

        _sb_patch(
            self._url, self._key, "build_run_specs",
            f"run_id=eq.{run_uuid}&spec_slug=eq.{urllib.parse.quote(spec_slug)}",
            patch,
        )
        self._emit_state_event(
            run_uuid, run_id, spec_slug, prior_status, new_status, park_reason or "", session_id
        )

    def _emit_state_event(
        self,
        run_uuid: str,
        run_id: str,
        spec_slug: str,
        from_status: str,
        to_status: str,
        reason: str,
        session_id: str | None,
    ) -> None:
        payload = [{
            "event_type": "build_run_transition",
            "entity_type": "build_run_spec",
            "entity_slug": spec_slug,
            "actor": "foreman_runner",
            "provenance_actor": "system_auto",
            "session_id": session_id,
            "intent": (
                f"build_run_spec '{spec_slug}' "
                f"{from_status} -> {to_status} in run {run_id}"
                + (f"; {reason}" if reason else "")
            ),
            "provenance_extra": json.dumps({
                "run_id": run_uuid,
                "text_run_id": run_id,
                "spec_slug": spec_slug,
                "from_status": from_status,
                "to_status": to_status,
                "reason": reason,
            }),
        }]
        try:
            _sb_post(self._url, self._key, "state_events", payload)
        except Exception as exc:
            print(f"  [WARN] state_events write failed: {exc}", file=sys.stderr)

    def update_data(self, run_uuid: str, spec_slug: str, fields: dict) -> None:
        fields = {**fields, "updated_at": datetime.now(timezone.utc).isoformat()}
        # build_run_specs' column is 'attempt'; 'attempt_no' belongs to the
        # separate foreman_tasks table. Normalize so a caller passing the
        # foreman_tasks-style key does not 400 PostgREST on an unknown column.
        if "attempt_no" in fields:
            fields["attempt"] = fields.pop("attempt_no")
        # A bookkeeping write must never kill the run: degrade to a [WARN] like
        # the sibling state_events write, not propagate (incident fm-20260709-1558).
        try:
            _sb_patch(
                self._url, self._key, "build_run_specs",
                f"run_id=eq.{run_uuid}&spec_slug=eq.{urllib.parse.quote(spec_slug)}",
                fields,
            )
        except Exception as exc:
            print(f"  [WARN] update_data write failed: {exc}", file=sys.stderr)

    def update_run_status(self, run_uuid: str, new_status: str, report: dict | None = None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        patch: dict = {"status": new_status, "updated_at": now}
        if new_status in ("completed", "failed", "cancelled"):
            patch["completed_at"] = now
        if report is not None:
            patch["report"] = json.dumps(report)
        _sb_patch(self._url, self._key, "build_runs", f"id=eq.{run_uuid}", patch)

    def list_stale_running_runs(self, older_than_hours: int = 24) -> list[dict]:
        """Return build_runs stuck in 'running' whose started_at (or created_at)
        is older than the cutoff. Used by the F5 zombie reconciler."""
        cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=older_than_hours)
        ).isoformat()
        return _sb_get(
            self._url, self._key, "build_runs",
            f"status=eq.running"
            f"&or=(started_at.lt.{urllib.parse.quote(cutoff)},"
            f"and(started_at.is.null,created_at.lt.{urllib.parse.quote(cutoff)}))"
            f"&select=id,run_id,status,started_at,created_at&order=created_at.asc",
        )

    def list_running_runs(self) -> list[dict]:
        """All build_runs currently in 'running', regardless of age. Used by the
        kill reconciler, which decides liveness from task heartbeats not run age."""
        return _sb_get(
            self._url, self._key, "build_runs",
            "status=eq.running"
            "&select=id,run_id,status,started_at,created_at&order=created_at.asc",
        )

    # ------------------------------------------------------------------
    # Phase 2: foreman_tasks layer
    # ------------------------------------------------------------------

    def create_task_row(
        self,
        run_id: str,
        spec_slug: str,
        build_order: int,
        depends_on: list[str],
        independent: bool,
        session_id: str,
    ) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        payload = {
            "run_id": run_id,
            "spec_slug": spec_slug,
            "build_order": build_order,
            "depends_on": depends_on,
            "independent": independent,
            "session_id": session_id,
            "status": "queued",
            "attempt_no": 0,
            "created_at": now,
            "updated_at": now,
        }
        rows = _sb_post(self._url, self._key, "foreman_tasks", [payload])
        return rows[0]

    def update_task_status(
        self,
        run_id: str,
        spec_slug: str,
        new_status: str,
        **fields: Any,
    ) -> None:
        # Single atomic RPC updates foreman_tasks AND upserts the matching
        # build_run_specs row (resolving the uuid FK server-side) in one
        # transaction. Replaces the prior foreman_tasks-only PATCH that severed
        # the ledger (incident fm-20260702-1841-50cc8a). jsonb columns are passed
        # as native objects; scalars as-is.
        p_fields = {k: v for k, v in fields.items() if v is not None}
        _sb_rpc(
            self._url, self._key, "foreman_transition_task",
            {
                "p_run_id": run_id,
                "p_spec_slug": spec_slug,
                "p_new_status": new_status,
                "p_fields": p_fields,
            },
        )

    def claim_task(
        self,
        run_id: str,
        spec_slug: str,
        session_id: str,
    ) -> bool:
        full = f"{self._url}/rest/v1/rpc/claim_foreman_task"
        payload = {"p_run_id": run_id, "p_spec_slug": spec_slug, "p_session_id": session_id}
        h = {**_sb_headers(self._key)}
        req = urllib.request.Request(
            full,
            data=json.dumps(payload).encode(),
            headers=h,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
        claimed = bool(result)
        return claimed

    def list_resumable_tasks(self, run_id: str) -> list[dict]:
        terminal = "committed,parked,dependent-halted"
        rows = _sb_get(
            self._url, self._key, "foreman_tasks",
            f"run_id=eq.{urllib.parse.quote(run_id)}"
            f"&status=not.in.({terminal})"
            f"&order=build_order.asc",
        )
        return rows

    def list_run_tasks(self, run_id: str) -> list[dict]:
        """All foreman_tasks rows for a run (any status). Used by the kill reconciler."""
        return _sb_get(
            self._url, self._key, "foreman_tasks",
            f"run_id=eq.{urllib.parse.quote(run_id)}&order=build_order.asc",
        )

    def has_pass_ledger_row(self, run_id: str, spec_slug: str) -> bool:
        """verify_result='PASS' build_run_specs row exists for this task."""
        rows = _sb_get(
            self._url, self._key, "build_run_specs",
            f"text_run_id=eq.{urllib.parse.quote(run_id)}"
            f"&spec_slug=eq.{urllib.parse.quote(spec_slug)}"
            f"&verify_result=eq.PASS&select=id&limit=1",
        )
        return bool(rows)

    def query_non_terminal_tasks(self, spec_slugs: list[str]) -> list[dict]:
        if not spec_slugs:
            return []
        slug_list = ",".join(urllib.parse.quote(s) for s in spec_slugs)
        return _sb_get(
            self._url, self._key, "foreman_tasks",
            f"spec_slug=in.({slug_list})"
            f"&status=in.(queued,building,verifying,merging)"
            f"&order=created_at.desc",
        )

    def emit_run_event(
        self,
        run_id: str,
        event: str,
        *,
        task_id: str | None = None,
        spec_slug: str | None = None,
        detail: dict | None = None,
    ) -> None:
        try:
            redacted = _redact_detail(detail) if detail else None
            payload = {
                "run_id": run_id,
                "task_id": task_id,
                "spec_slug": spec_slug,
                "event": event,
                "detail": redacted,
            }
            _sb_post(self._url, self._key, "foreman_run_events", [payload])
        except Exception as exc:
            print(f"[run-event] emit failed: {exc}", file=sys.stderr)

    def fetch_run_events(self, run_id: str) -> list[dict]:
        """Read foreman_run_events for a run, ordered by emitted_at asc."""
        return _sb_get(
            self._url, self._key, "foreman_run_events",
            f"run_id=eq.{urllib.parse.quote(run_id)}&order=emitted_at.asc",
        )

    def recent_dispositions(self, run_id: str, spec_slug: str, limit: int) -> list[str]:
        rows = _sb_get(
            self._url, self._key, "foreman_run_events",
            f"run_id=eq.{urllib.parse.quote(run_id)}"
            f"&spec_slug=eq.{urllib.parse.quote(spec_slug)}"
            f"&event=eq.parked"
            f"&order=emitted_at.desc&limit={int(limit)}",
        )
        out: list[str] = []
        for r in rows:
            pr = (r.get("detail") or {}).get("park_reason")
            if pr:
                out.append(pr)
        return out

    def fetch_prior_verify_findings(self, spec_slug: str, current_run_id: str) -> str | None:
        """find the most recent verify findings from a prior run for this spec.

        Queries foreman_run_events for parked events with verify-failed-retry or
        spec-wallclock-ceiling-exceeded, excluding the current run, ordered by
        emitted_at desc to get the most recent. Returns None when none exist."""
        _retryable_reasons = "verify-failed-retry,spec-wallclock-ceiling-exceeded"
        try:
            rows = _sb_get(
                self._url, self._key, "foreman_run_events",
                f"spec_slug=eq.{urllib.parse.quote(spec_slug)}"
                f"&event=eq.parked"
                f"&order=emitted_at.desc&limit=10",
            )
        except Exception:
            return None
        for row in rows:
            if (row.get("run_id") or "") == current_run_id:
                continue
            detail = row.get("detail") or {}
            park_reason = detail.get("park_reason") or ""
            if park_reason not in ("verify-failed-retry", "spec-wallclock-ceiling-exceeded"):
                continue
            ft = detail.get("failure_trail") or {}
            for v in ft.values():
                if isinstance(v, dict) and v.get("findings"):
                    return str(v["findings"])
        return None

    def patch_heartbeat(self, run_id: str, spec_slug: str, timestamp: str) -> None:
        """Write last_heartbeat_at on the live task row without touching status.

        Non-blocking and failure-tolerant: a failed heartbeat write logs to stderr and
        never raises into the supervision loop. The single-flight guard and
        stale-claim recovery read this same column unchanged.
        """
        try:
            _sb_patch(
                self._url, self._key, "foreman_tasks",
                f"run_id=eq.{urllib.parse.quote(run_id)}"
                f"&spec_slug=eq.{urllib.parse.quote(spec_slug)}",
                {"last_heartbeat_at": timestamp},
            )
        except Exception as exc:
            print(f"[heartbeat] patch failed for {spec_slug!r}: {exc}", file=sys.stderr)

    # ------------------------------------------------------------------
    # R1: live queue append support
    # ------------------------------------------------------------------

    def fetch_run(self, run_id: str) -> dict | None:
        """Fetch a build_runs row by its text run_id (liveness check)."""
        rows = _sb_get(
            self._url, self._key, "build_runs",
            f"run_id=eq.{urllib.parse.quote(run_id)}"
            f"&select=id,run_id,status,bundle,ordered_bundle&limit=1",
        )
        return rows[0] if rows else None

    def update_run_bundle(self, run_uuid: str, bundle: list[str], ordered_bundle: list[str]) -> None:
        """Update build_runs.bundle/ordered_bundle when specs are appended."""
        now = datetime.now(timezone.utc).isoformat()
        _sb_patch(
            self._url, self._key, "build_runs", f"id=eq.{run_uuid}",
            {"bundle": json.dumps(bundle), "ordered_bundle": json.dumps(ordered_bundle),
             "updated_at": now},
        )
