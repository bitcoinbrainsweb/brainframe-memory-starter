#!/usr/bin/env python3
"""Quickstart demo: the sessions handoff chain and pickup state machine, on SQLite.

No accounts, no network, no keys. This creates the `sessions` table (the subset of
harness/sessions/SCHEMA.sql that carries the handoff chain) in a throwaway SQLite
file and scripts a real lifecycle: open session A, write a handoff brief and a
pickup slug, hand A off, then open session B that claims the slug (the pickup) and
inherits the chain. It reads the chain back from the DB -- the actual state
machine, not a mock.

The real path (harness/sessions/SETUP.md) runs this on Postgres/Supabase with a
DEFERRABLE self-referencing foreign key, row-level security, and partial unique
indexes. Here we keep the same columns and the same transitions, drop the pg-only
bits, and keep the pickup_slug uniqueness guard as a real partial unique index
(SQLite supports it).

    python3 harness/quickstart/demo_sessions.py
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import uuid
from datetime import datetime, timedelta, timezone

# --- Portable subset of harness/sessions/SCHEMA.sql -----------------------------
# Dropped for SQLite: the update_updated_at trigger, ENABLE ROW LEVEL SECURITY /
# policies, and the DEFERRABLE self-FK on handoff_chain_id (kept as a plain TEXT
# column carrying the same chain-head/inheritance values). Kept: the CHECK on
# status, and the partial unique index that enforces "one live holder of a
# pickup_slug per project" -- the collision guard handchat depends on.
SCHEMA = """
CREATE TABLE sessions (
  id                TEXT PRIMARY KEY,
  project           TEXT NOT NULL,
  chat_title        TEXT,
  started_at        TEXT NOT NULL,
  ended_at          TEXT,
  last_seen_at      TEXT NOT NULL,
  status            TEXT NOT NULL CHECK (status IN ('active','closed','handed_off')),
  handoff_chain_id  TEXT NOT NULL,     -- chain head points at its own id; continuations inherit it
  context_brief     TEXT,              -- generous handoff brief written by handchat, read by pickup
  pickup_slug       TEXT,              -- short handle set by handchat, consumed by pickup
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL
);
-- pickup_slug is unique per project among sessions that still hold one.
CREATE UNIQUE INDEX uq_sessions_pickup_slug
  ON sessions (project, pickup_slug) WHERE pickup_slug IS NOT NULL;
CREATE INDEX idx_sessions_handoff_chain ON sessions (handoff_chain_id) WHERE status != 'closed';
"""

PROJECT = "project_a"  # SCHEMA.sql ships a CHECK on your own project slugs


def _ts(offset_s: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_s)).isoformat()


def narrate(line: str) -> None:
    print(f"  {line}")


def main() -> int:
    print("=" * 70)
    print("SESSIONS QUICKSTART -- handoff chain + pickup state machine on SQLite")
    print("=" * 70)

    db_path = os.path.join(tempfile.mkdtemp(prefix="qs_sessions_"), "sessions_demo.sqlite3")
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    narrate(f"created sessions table in {db_path}")

    # 1. Open session A. A fresh chain head points handoff_chain_id at its own id.
    a_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO sessions (id, project, chat_title, started_at, last_seen_at, status, "
        "handoff_chain_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (a_id, PROJECT, "Acme onboarding, part 1", _ts(0), _ts(0), "active", a_id, _ts(0), _ts(0)),
    )
    conn.commit()
    narrate(f"session A opened (active), chain head -> itself  id={a_id[:8]}")

    # 2. handchat: A pauses. Write the handoff brief + a pickup slug, mark handed_off.
    #    Note the row does NOT get deleted or hard-closed: it goes idle/handed_off and
    #    keeps its state, which is how the next chat reconstructs context.
    slug = "acme-onboarding-2"
    conn.execute(
        "UPDATE sessions SET context_brief=?, pickup_slug=?, status='handed_off', "
        "ended_at=?, last_seen_at=?, updated_at=? WHERE id=?",
        (
            "Finished Acme account setup and the first widget import. "
            "Next: wire the billing webhook and add the retry test.",
            slug, _ts(10), _ts(10), _ts(10), a_id,
        ),
    )
    conn.commit()
    narrate(f"handchat: A wrote context_brief + pickup_slug='{slug}', status -> handed_off")

    # The unique index now guards the slug: a second live holder is rejected.
    try:
        conn.execute(
            "INSERT INTO sessions (id, project, chat_title, started_at, last_seen_at, status, "
            "handoff_chain_id, pickup_slug, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), PROJECT, "collision", _ts(11), _ts(11), "active",
             str(uuid.uuid4()), slug, _ts(11), _ts(11)),
        )
        conn.commit()
        narrate("WARNING: collision guard did NOT fire (unexpected)")
    except sqlite3.IntegrityError:
        narrate(f"collision guard held: a second live session cannot claim '{slug}'")
        conn.rollback()

    # 3. pickup: open session B in a new chat, claim the slug, inherit A's chain.
    a_chain, a_brief = conn.execute(
        "SELECT handoff_chain_id, context_brief FROM sessions WHERE pickup_slug=? AND status='handed_off'",
        (slug,),
    ).fetchone()
    b_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO sessions (id, project, chat_title, started_at, last_seen_at, status, "
        "handoff_chain_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (b_id, PROJECT, "Acme onboarding, part 2", _ts(20), _ts(20), "active", a_chain, _ts(20), _ts(20)),
    )
    # Slug consumed: clear it on A so the guard frees up and it is not re-claimed.
    conn.execute("UPDATE sessions SET pickup_slug=NULL, updated_at=? WHERE id=?", (_ts(20), a_id))
    conn.commit()
    narrate(f"pickup: B opened (active), inherited chain {a_chain[:8]}, read A's brief:")
    narrate(f'        "{a_brief}"')

    # 4. Read the whole chain back from the DB -- one query on handoff_chain_id.
    print("\nHandoff chain (one thread of work spanning two chats):")
    print(f"    {'started_at':26} {'status':11} {'title':26} slug")
    for started, status, title, ps in conn.execute(
        "SELECT started_at, status, chat_title, pickup_slug FROM sessions "
        "WHERE handoff_chain_id=? ORDER BY started_at ASC",
        (a_chain,),
    ):
        print(f"    {started:26} {status:11} {title:26} {ps or '-'}")

    # Idle-not-closed: A is still queryable as handed_off, not deleted.
    a_status = conn.execute("SELECT status FROM sessions WHERE id=?", (a_id,)).fetchone()[0]
    print(f"\nSession A is still on record as '{a_status}' (idle-not-closed), reachable in the chain.")
    print("Real path: harness/sessions/SETUP.md (Postgres/Supabase, DEFERRABLE self-FK, RLS).")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
