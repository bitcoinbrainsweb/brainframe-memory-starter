#!/usr/bin/env python3
"""Quickstart demo: the memory system's append-only event log, on local SQLite.

No accounts, no network, no keys. This creates the `state_events` table (the same
shape as harness/memory/SCHEMA.sql, translated to portable SQLite types) in a
throwaway file, writes a handful of events the way the real system would, and
reads them back to show the audit-trail shape.

The real path (harness/memory/SETUP.md) runs this table on Postgres/Supabase with
JSONB, row-level security, and trigram search. Here we use TEXT for the JSON
snapshots and skip the pg-only bits (extension, trigger, RLS) so it runs anywhere
Python 3 does.

    python3 harness/quickstart/demo_memory.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import uuid
from datetime import datetime, timedelta, timezone

# --- Portable subset of harness/memory/SCHEMA.sql -------------------------------
# Dropped for SQLite: CREATE EXTENSION pg_trgm, the update_updated_at trigger,
# ENABLE ROW LEVEL SECURITY / policies, and gen_random_uuid() (uuids minted in
# Python). jsonb -> TEXT (JSON serialized with json.dumps). timestamptz -> TEXT
# (ISO-8601). The column set and the append-only intent are unchanged.
SCHEMA = """
CREATE TABLE state_events (
  id           TEXT PRIMARY KEY,
  entity_type  TEXT NOT NULL,          -- 'decision', 'note', 'session', 'card'
  entity_id    TEXT NOT NULL,
  entity_slug  TEXT,                   -- optional human-readable handle
  event_type   TEXT NOT NULL,          -- 'created', 'updated', 'closed'
  actor        TEXT NOT NULL,          -- who caused the change
  before       TEXT,                   -- entity snapshot before (JSON; null on create)
  after        TEXT,                   -- entity snapshot after (JSON; null on delete)
  session_id   TEXT,                   -- optional link to the sessions system
  created_at   TEXT NOT NULL
);
CREATE INDEX idx_state_events_entity ON state_events (entity_type, entity_id);
CREATE INDEX idx_state_events_created_desc ON state_events (created_at DESC);
"""


def _now_iso(offset_s: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_s)).isoformat()


def append_event(conn, *, entity_type, entity_id, entity_slug, event_type, actor,
                 before, after, created_at, session_id=None):
    """Append one immutable event. The log is append-only: we never UPDATE or
    DELETE a row, we write a new event that supersedes the last one."""
    conn.execute(
        "INSERT INTO state_events "
        "(id, entity_type, entity_id, entity_slug, event_type, actor, before, after, session_id, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            str(uuid.uuid4()), entity_type, entity_id, entity_slug, event_type, actor,
            json.dumps(before) if before is not None else None,
            json.dumps(after) if after is not None else None,
            session_id, created_at,
        ),
    )


def narrate(line: str) -> None:
    print(f"  {line}")


def main() -> int:
    print("=" * 70)
    print("MEMORY QUICKSTART -- append-only event log on local SQLite")
    print("=" * 70)

    db_path = os.path.join(tempfile.mkdtemp(prefix="qs_memory_"), "memory_demo.sqlite3")
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    narrate(f"created state_events table in {db_path}")

    # One tracked entity: a decision that gets made, then revised. Each change is a
    # new event carrying the full before/after snapshot -- that is the substance.
    decision_id = str(uuid.uuid4())
    session_id = str(uuid.uuid4())

    print("\nWriting events...")
    v1 = {"title": "Adopt SQLite for the quickstart", "status": "proposed", "owner": "acme-agent"}
    append_event(
        conn, entity_type="decision", entity_id=decision_id, entity_slug="adopt-sqlite",
        event_type="created", actor="acme-agent", before=None, after=v1,
        created_at=_now_iso(0), session_id=session_id,
    )
    narrate("decision 'adopt-sqlite' created (proposed)")

    v2 = {**v1, "status": "accepted", "rationale": "stdlib, zero-install, portable"}
    append_event(
        conn, entity_type="decision", entity_id=decision_id, entity_slug="adopt-sqlite",
        event_type="updated", actor="acme-reviewer", before=v1, after=v2,
        created_at=_now_iso(1), session_id=session_id,
    )
    narrate("decision 'adopt-sqlite' updated (proposed -> accepted) by acme-reviewer")

    note = {"body": "Quickstart must stay dependency-free", "pinned": True}
    append_event(
        conn, entity_type="note", entity_id=str(uuid.uuid4()), entity_slug="no-deps",
        event_type="created", actor="acme-agent", before=None, after=note,
        created_at=_now_iso(2), session_id=session_id,
    )
    narrate("note 'no-deps' created")

    conn.commit()

    # Read-back 1: the raw event log, newest first (idx_state_events_created_desc).
    print("\nEvent log (newest first):")
    print(f"    {'created_at':26} {'entity':10} {'event':9} {'actor':14} slug")
    for row in conn.execute(
        "SELECT created_at, entity_type, event_type, actor, entity_slug "
        "FROM state_events ORDER BY created_at DESC"
    ):
        ca, et, ev, ac, slug = row
        print(f"    {ca:26} {et:10} {ev:9} {ac:14} {slug}")

    # Read-back 2: reconstruct one entity's history from its events. This is why the
    # log is the source of truth: the current state is the `after` of its last event.
    print("\nHistory of decision 'adopt-sqlite' (before -> after per event):")
    for ev_type, before, after in conn.execute(
        "SELECT event_type, before, after FROM state_events "
        "WHERE entity_type='decision' AND entity_id=? ORDER BY created_at ASC",
        (decision_id,),
    ):
        b = json.loads(before) if before else None
        a = json.loads(after) if after else None
        b_status = b["status"] if b else "(none)"
        a_status = a["status"] if a else "(none)"
        narrate(f"[{ev_type}] status {b_status} -> {a_status}")
    current = conn.execute(
        "SELECT after FROM state_events WHERE entity_type='decision' AND entity_id=? "
        "ORDER BY created_at DESC LIMIT 1",
        (decision_id,),
    ).fetchone()
    narrate(f"current state = last event's 'after' = {json.loads(current[0])}")

    total = conn.execute("SELECT COUNT(*) FROM state_events").fetchone()[0]
    print(f"\nDone. {total} immutable events recorded; nothing was updated or deleted.")
    print("Real path: harness/memory/SETUP.md (Postgres/Supabase, JSONB, RLS, trigram search).")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
