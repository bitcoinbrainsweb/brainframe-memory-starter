"""Reconcile zombie build_runs.

BundleRunner finalizes build_runs.status in a finally-path, but rows stranded by
crashes before that path runs need a sweep. This marks any run still 'running'
past a cutoff (default 24h) as 'failed' so dashboards and the single-flight lock
stop treating dead runs as live.

CLI (with SUPABASE_URL and SUPABASE_SERVICE_KEY in the environment):
    python -m harness.foreman.scripts.reconcile [--hours 24] [--dry-run]
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

from .heartbeat import heartbeat_stale_threshold_s
from .ledger import LedgerBackend

_TASK_TERMINAL = {"committed", "parked", "dependent-halted"}


def reconcile_stale_runs(
    ledger: LedgerBackend,
    older_than_hours: int = 24,
    dry_run: bool = False,
) -> list[dict]:
    """Mark 'running' build_runs older than the cutoff as 'failed'.

    Returns the list of reconciled run descriptors. Pure w.r.t. the ledger:
    reads via list_stale_running_runs, writes via update_run_status.
    """
    stale = ledger.list_stale_running_runs(older_than_hours)
    reconciled: list[dict] = []
    for r in stale:
        rid = r.get("id")
        desc = {"id": rid, "run_id": r.get("run_id"),
                "started_at": r.get("started_at") or r.get("created_at")}
        if not dry_run and rid:
            ledger.update_run_status(
                rid, "failed",
                report={"reconciled": "stale-running-reaper",
                        "older_than_hours": older_than_hours},
            )
        reconciled.append(desc)
    return reconciled


def _parse_ts(value) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _task_activity(task: dict) -> datetime | None:
    """Most-recent sign of life for a task: heartbeat, then claim, then update."""
    latest: datetime | None = None
    for key in ("last_heartbeat_at", "claimed_at", "updated_at"):
        dt = _parse_ts(task.get(key))
        if dt is not None and (latest is None or dt > latest):
            latest = dt
    return latest


def reconcile_killed_runs(
    ledger: LedgerBackend,
    stale_threshold_s: int | None = None,
    *,
    exclude_run_id: str | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
) -> list[dict]:
    """Close build_runs stranded 'running' by a hard kill (F5 / kill-reconciliation).

    A run is orphaned when it has at least one non-terminal task and none of its
    non-terminal tasks has shown a heartbeat/claim/update newer than the stale
    threshold -- i.e. every worker on it is dead. Such runs are flipped to
    'aborted' with a report noting the reconciliation and the last-seen timestamps.

    Unlike reconcile_stale_runs (a 24h age reaper), this keys off task heartbeats,
    so it catches a just-killed run that a finally-block finalize would have missed.
    Reuses the heartbeat stale threshold. Returns the reconciled run descriptors.
    """
    if stale_threshold_s is None:
        stale_threshold_s = heartbeat_stale_threshold_s()
    now = now or datetime.now(timezone.utc)

    reconciled: list[dict] = []
    # Every currently-'running' build_run, recent or not: liveness is decided from
    # task heartbeats below, not run age (a kill can strand a run seconds old).
    for run in ledger.list_running_runs():
        run_uuid = run.get("id")
        text_run_id = run.get("run_id")
        if not run_uuid or (exclude_run_id and text_run_id == exclude_run_id):
            continue

        tasks = ledger.list_run_tasks(text_run_id) if text_run_id else []
        non_terminal = [t for t in tasks if t.get("status") not in _TASK_TERMINAL]
        if not non_terminal:
            # No in-flight work to prove liveness either way; leave for the 24h
            # age reaper rather than abort a run that may be mid-intake.
            continue

        activities = [_task_activity(t) for t in non_terminal]
        newest = max((a for a in activities if a is not None), default=None)
        is_orphaned = newest is None or (now - newest).total_seconds() > stale_threshold_s
        if not is_orphaned:
            continue

        desc = {
            "id": run_uuid,
            "run_id": text_run_id,
            "last_task_activity": newest.isoformat() if newest else None,
            "stale_tasks": [t.get("spec_slug") for t in non_terminal],
        }
        if not dry_run:
            ledger.update_run_status(
                run_uuid, "aborted",
                report={
                    "reconciled": "kill-orphan-reconciliation",
                    "run_id": text_run_id,
                    "aborted_at": now.isoformat(),
                    "last_task_activity": desc["last_task_activity"],
                    "stale_tasks": desc["stale_tasks"],
                    "stale_threshold_s": stale_threshold_s,
                },
            )
        reconciled.append(desc)
    return reconciled


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="harness.foreman.scripts.reconcile",
        description="Mark stale 'running' build_runs as failed (F5 zombie reaper).",
    )
    parser.add_argument("--hours", type=int, default=24,
                        help="Age cutoff in hours (default 24).")
    parser.add_argument("--dry-run", action="store_true", default=False,
                        help="List what would be reconciled without writing.")
    args = parser.parse_args(argv)

    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        print("ERROR: SUPABASE_URL and SUPABASE_SERVICE_KEY required "
              "(set them in your environment).", file=sys.stderr)
        return 2

    from .ledger import SupabaseLedger
    ledger = SupabaseLedger(url, key)
    reconciled = reconcile_stale_runs(ledger, older_than_hours=args.hours, dry_run=args.dry_run)

    verb = "would reconcile" if args.dry_run else "reconciled"
    print(f"{verb} {len(reconciled)} stale running run(s) older than {args.hours}h:")
    for d in reconciled:
        print(f"  {d.get('run_id') or d['id']}  (started {d.get('started_at')})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
