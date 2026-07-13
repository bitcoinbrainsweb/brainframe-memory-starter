"""TaskQueue: durable task state machine for the build phase."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from scripts.foreman.heartbeat import heartbeat_stale_threshold_s
from scripts.foreman.ledger import LedgerBackend
from scripts.foreman.models import (
    HaltChain,
    HaltRecord,
    InvalidTransition,
    TaskStatus,
    VALID_TRANSITIONS,
)


class TaskQueue:
    def __init__(self, ledger: LedgerBackend, run_id: str, session_id: str) -> None:
        self._ledger = ledger
        self._run_id = run_id
        self._session_id = session_id

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------

    def next_queued(self) -> dict | None:
        """Return the lowest build_order queued task, or None if none remain.

        For InMemoryLedger: reads _task_rows directly (in-process, no network).
        For SupabaseLedger: re-queries the DB each call to get authoritative state.
        """
        rows = getattr(self._ledger, "_task_rows", None)
        if rows is not None:
            candidates = [
                row for (rid, _), row in rows.items()
                if rid == self._run_id and row["status"] == "queued"
            ]
        else:
            resumable = self._ledger.list_resumable_tasks(self._run_id)
            candidates = [r for r in resumable if r.get("status") == "queued"]
        if not candidates:
            return None
        return min(candidates, key=lambda r: r.get("build_order", 0))

    # ------------------------------------------------------------------
    # Claim
    # ------------------------------------------------------------------

    def claim(self, task: dict) -> bool:
        """Atomically claim task (write-ahead: DB transitions to building before action).

        Returns True if claim succeeded; False if task was already claimed.
        Writes both foreman_tasks (via RPC) and build_run_specs.
        Mutates task dict in-place so callers see the updated status.
        """
        ok = self._ledger.claim_task(self._run_id, task["spec_slug"], self._session_id)
        if ok:
            task["status"] = TaskStatus.BUILDING.value
            task["claimed_by"] = self._session_id
        return ok

    # ------------------------------------------------------------------
    # Advance (write-ahead transition enforced)
    # ------------------------------------------------------------------

    def advance(self, task: dict, new_status: TaskStatus, **fields) -> None:
        """Write-ahead transition: update foreman_tasks + insert build_run_specs row.

        Must be called BEFORE the action described by new_status.
        Raises InvalidTransition on illegal moves.
        """
        current = TaskStatus(task["status"])
        allowed = VALID_TRANSITIONS.get(current, set())
        if new_status not in allowed:
            raise InvalidTransition(
                f"Cannot transition {task['spec_slug']!r} from {current!r} to {new_status!r}. "
                f"Allowed: {[s.value for s in allowed]}"
            )
        task["status"] = new_status.value
        for k, v in fields.items():
            task[k] = v
        self._ledger.update_task_status(self._run_id, task["spec_slug"], new_status.value, **fields)

    # ------------------------------------------------------------------
    # Park (terminal failure) + dependent halting
    # ------------------------------------------------------------------

    def park(self, task: dict, reason: str, failure_trail: dict) -> list[HaltRecord]:
        """Transition task to parked, then recursively halt dependents."""
        current = TaskStatus(task["status"])
        allowed = VALID_TRANSITIONS.get(current, set())
        if TaskStatus.PARKED not in allowed:
            raise InvalidTransition(
                f"Cannot park {task['spec_slug']!r} from {current!r}"
            )
        task["status"] = TaskStatus.PARKED.value
        task["park_reason"] = reason
        task["failure_trail"] = failure_trail
        self._ledger.update_task_status(
            self._run_id,
            task["spec_slug"],
            TaskStatus.PARKED.value,
            park_reason=reason,
            failure_trail=failure_trail,
        )
        return self.halt_dependents(task["spec_slug"])

    def halt_dependents(self, parked_slug: str) -> list[HaltRecord]:
        """Recursively set dependent-halted on all tasks that depend on parked_slug.

        Uses visited-set to prevent infinite loop on malformed depends_on.
        Returns HaltRecord list for report.
        """
        halt_records: list[HaltRecord] = []
        visited: set[str] = set()
        queue = [parked_slug]
        rows_dict = getattr(self._ledger, "_task_rows", None)
        while queue:
            cause_slug = queue.pop()
            if cause_slug in visited:
                continue
            visited.add(cause_slug)
            if rows_dict is not None:
                task_iter = [
                    (slug, row) for (rid, slug), row in rows_dict.items()
                    if rid == self._run_id
                ]
            else:
                task_iter = [
                    (r["spec_slug"], r) for r in self._ledger.list_resumable_tasks(self._run_id)
                ]
            for slug, row in task_iter:
                if slug in visited:
                    continue
                if row.get("independent"):
                    continue
                dep_list = row.get("depends_on") or []
                if cause_slug in dep_list and row["status"] not in (
                    TaskStatus.COMMITTED.value,
                    TaskStatus.PARKED.value,
                    TaskStatus.DEPENDENT_HALTED.value,
                ):
                    if rows_dict is not None:
                        row["status"] = TaskStatus.DEPENDENT_HALTED.value
                    self._ledger.update_task_status(
                        self._run_id, slug, TaskStatus.DEPENDENT_HALTED.value
                    )
                    halt_records.append(HaltRecord(spec_slug=slug, halted_because=cause_slug))
                    queue.append(slug)
        return halt_records

    # ------------------------------------------------------------------
    # Stale-claim recovery
    # ------------------------------------------------------------------

    def recover_stale(self) -> list[str]:
        """Apply stale-claim recovery before re-polling after crash.

        - Rows with commit_sha IS NOT NULL: transition to committed (OQ-F2-2).
        - Rows with status IN (building, verifying, merging, ci-gating) AND stale heartbeat:
          - ci-gating with ci_verdict populated: advance to merging (gate result already known)
          - otherwise: reset to queued, clear claimed_by
        - Stale threshold:
          - last_heartbeat_at present: FOREMAN_STALE_MISS_COUNT * FOREMAN_HEARTBEAT_INTERVAL_S (default 180s)
          - last_heartbeat_at NULL (pre-Phase-3 row): 30-minute fallback

        Returns list of recovered spec_slugs.
        """
        recovered: list[str] = []
        heartbeat_threshold_s = heartbeat_stale_threshold_s()
        fallback_threshold = datetime.now(timezone.utc) - timedelta(minutes=30)
        in_progress = {
            TaskStatus.BUILDING.value,
            TaskStatus.VERIFYING.value,
            TaskStatus.MERGING.value,
            TaskStatus.CI_GATING.value,
        }

        rows_dict = getattr(self._ledger, "_task_rows", None)
        if rows_dict is not None:
            task_iter = [
                (slug, row) for (rid, slug), row in rows_dict.items()
                if rid == self._run_id
            ]
        else:
            task_iter = [
                (r["spec_slug"], r) for r in self._ledger.list_resumable_tasks(self._run_id)
            ]

        now = datetime.now(timezone.utc)

        for slug, row in task_iter:
            if row.get("commit_sha") and row["status"] != TaskStatus.COMMITTED.value:
                # F1: only auto-commit a crashed row if a verify_result='PASS'
                # ledger row exists. commit_sha is written at the VERIFYING
                # write-ahead (before verify runs), so a commit_sha alone does
                # NOT prove the build passed. Without PASS evidence, reset to
                # queued for a clean rebuild+verify (origin ground-truth will
                # short-circuit via ALREADY_CONTAINS if the commit is on main).
                has_pass = getattr(self._ledger, "has_pass_ledger_row", None)
                pass_ok = has_pass(self._run_id, slug) if callable(has_pass) else True
                if pass_ok:
                    if rows_dict is not None:
                        row["status"] = TaskStatus.COMMITTED.value
                    self._ledger.update_task_status(self._run_id, slug, TaskStatus.COMMITTED.value)
                    recovered.append(slug)
                    continue
                if rows_dict is not None:
                    row["status"] = TaskStatus.QUEUED.value
                    row["claimed_by"] = None
                    row["claimed_at"] = None
                self._ledger.update_task_status(
                    self._run_id, slug, TaskStatus.QUEUED.value,
                    claimed_by=None, claimed_at=None,
                )
                recovered.append(slug)
                continue

            if row["status"] not in in_progress:
                continue

            # Determine stale threshold: heartbeat-based or 30-min fallback
            last_hb = row.get("last_heartbeat_at")
            if last_hb:
                try:
                    hb_dt = datetime.fromisoformat(last_hb)
                    if hb_dt.tzinfo is None:
                        hb_dt = hb_dt.replace(tzinfo=timezone.utc)
                    is_stale = (now - hb_dt).total_seconds() > heartbeat_threshold_s
                except ValueError:
                    is_stale = False
            else:
                # Pre-Phase-3 row: fall back to claimed_at + 30-min window
                claimed_at = row.get("claimed_at")
                is_stale = False
                if claimed_at:
                    try:
                        dt = datetime.fromisoformat(claimed_at)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        is_stale = dt < fallback_threshold
                    except ValueError:
                        pass

            if not is_stale:
                continue

            # ci-gating with ci_verdict already written → advance to merging
            if row["status"] == TaskStatus.CI_GATING.value and row.get("ci_verdict"):
                if rows_dict is not None:
                    row["status"] = TaskStatus.MERGING.value
                self._ledger.update_task_status(self._run_id, slug, TaskStatus.MERGING.value)
                recovered.append(slug)
                continue

            # All other stale in-progress statuses: reset to queued
            if rows_dict is not None:
                row["status"] = TaskStatus.QUEUED.value
                row["claimed_by"] = None
                row["claimed_at"] = None
            self._ledger.update_task_status(
                self._run_id, slug, TaskStatus.QUEUED.value,
                claimed_by=None, claimed_at=None,
            )
            recovered.append(slug)
        return recovered

    # ------------------------------------------------------------------
    # R5: orphaned in-flight task reaper
    # ------------------------------------------------------------------

    def reap_orphans(self, stale_minutes: int | None = None) -> list[str]:
        """Park tasks stuck in building/verifying past the staleness window.

        tasks left in-flight when a run was killed are parked with reason
        'orphaned-stale-claim' (an operational reason the R4 breaker excludes), so
        they never block a future run. Idempotent: a second call reaps nothing.
        Run-scoped. Returns the list of reaped spec_slugs.
        """
        import os as _os
        if stale_minutes is None:
            stale_minutes = int(_os.environ.get("FOREMAN_TASK_STALE_MINUTES", "30"))
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=stale_minutes)
        in_flight = {TaskStatus.BUILDING.value, TaskStatus.VERIFYING.value}

        rows_dict = getattr(self._ledger, "_task_rows", None)
        if rows_dict is not None:
            task_iter = [
                (slug, row) for (rid, slug), row in rows_dict.items()
                if rid == self._run_id
            ]
        else:
            task_iter = [
                (r["spec_slug"], r) for r in self._ledger.list_resumable_tasks(self._run_id)
            ]

        reaped: list[str] = []
        for slug, row in task_iter:
            if row.get("status") not in in_flight:
                continue
            claimed_at = row.get("claimed_at")
            if not claimed_at:
                continue
            try:
                dt = datetime.fromisoformat(claimed_at)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if dt < cutoff:
                if rows_dict is not None:
                    row["status"] = TaskStatus.PARKED.value
                    row["park_reason"] = "orphaned-stale-claim"
                self._ledger.update_task_status(
                    self._run_id, slug, TaskStatus.PARKED.value,
                    park_reason="orphaned-stale-claim",
                )
                reaped.append(slug)
        return reaped
