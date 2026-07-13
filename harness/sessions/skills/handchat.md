---
name: handchat
description: >-
  Mid-session pause handoff for immediate continuation in a new chat. Use when the
  operator types "handchat" or when a session-close routine invokes it under context
  pressure. Writes a context_brief and a pickup_slug to the current sessions row. A
  new chat resumes via `pickup {slug}`.
---

# Handchat: mid-session pause handoff

Store-only. One DB write to the current `sessions` row. No file writes, no session
close, no memory audit, no velocity write. Time budget: a few seconds.

## Precondition

`current_session_id` must be known from the boot-time `start_session()` call. If it
is missing, stop and surface: "no current_session_id, boot did not start the session
correctly, cannot handchat without one."

## Step 1: derive a topic slug

A 2 to 4 word, lowercase, kebab-case slug that uniquely describes what was being
worked on (for example `phase2-spec-writing`, `session-id-propagation`). It must be
specific enough to disambiguate from other open handoffs in the same project. Lead
with a project-specific term so your pickup routing sends it to the right project.

Detect collisions before writing. A pickup_slug is unique per project among sessions
that still hold one:

```sql
SELECT id, started_at
  FROM sessions
 WHERE project = '{project}'
   AND pickup_slug = '{candidate_slug}'
   AND pickup_slug IS NOT NULL;
```

If a row returns, another open handoff already owns this slug. Append a
disambiguator or pick a more specific slug.

## Step 2: write to the sessions row

A single UPDATE on the current session row, optimistic-locked on `updated_at`:

```sql
-- Re-read for the optimistic-lock predicate
SELECT id, updated_at FROM sessions WHERE id = '{current_session_id}';

-- Update
UPDATE sessions
   SET context_brief = :context_brief,   -- the distilled handoff intelligence, generous
       pickup_slug   = :pickup_slug,     -- the fuzzy-match handle
       last_seen_at  = now()
 WHERE id = '{current_session_id}'
   AND updated_at = '{old_ts}'
RETURNING id, pickup_slug;
```

If zero rows return, another writer raced you. Record the conflict as a state_events
row and retry once after re-reading:

```sql
INSERT INTO state_events (entity_type, entity_id, event_type, actor, after, session_id)
VALUES ('session', '{current_session_id}', 'write_conflict_detected', 'agent_{project}',
        jsonb_build_object('attempted_action','handchat_update','attempted_old_updated_at','{old_ts}'),
        '{current_session_id}');
```

If the unique index on `(project, pickup_slug)` rejects the write, a collision slipped
past Step 1. Surface it and pick a different slug. Do not retry silently.

## Step 3: write the context brief

`context_brief` is the whole point: a new chat reads it and becomes as capable as this
one. Begin with a one-sentence "Pick up here: ..." then be generous. No length cap.
Suggested sections: mental model, domain knowledge surfaced this session, decisions
and reasoning, approaches that failed, key writes this session, constraints
discovered, open questions.

## Step 4: derive chat names

Both names use the slug from Step 1:
- Outgoing (this chat): `-> {project} - {slug-spaced} - handoff`
- Incoming (new chat): `<- {project} - {slug-spaced} - pickup`

where `{slug-spaced}` renders the slug with spaces instead of dashes. The incoming
name must make the pickup target unambiguous.

## Step 5: surface

Print the pickup phrase (`pickup {slug}`), both chat names, and a note that the new
chat will be assigned a fresh session_id at boot with `handoff_chain_id` inherited
from this row.

## Failure mode

If the UPDATE fails (DB unreachable, retries exhausted): retry once after a short
wait, then surface the context_brief content inline so the operator can paste it into
a new chat by hand. Never silent-drop: a missing handoff means a corrupted chain.

## Chain resolution

When the session-close routine runs in the final chat of a chain, check whether this
session was a continuation (`handoff_chain_id != id`). If so, trace the chain and read
accumulated context:

```sql
SELECT id, summary, context_brief
  FROM sessions
 WHERE handoff_chain_id = '{chain_id}'
 ORDER BY started_at;
```

Write one unified summary covering the chain in the current row's `summary` field.

## Rules to preserve

1. One write per project: UPDATE sessions SET context_brief, pickup_slug, last_seen_at.
2. The context_brief is intelligence-oriented and generous. Invest in it.
3. pickup_slug is unique per project among unclaimed sessions; the partial unique index enforces it.
4. The slug is the single source of truth: chat names and the pickup phrase must agree.
5. Failure mode: retry once, fall back to surfacing inline content, never silent-drop.
6. No file writes: the state store is the single canonical writer.
