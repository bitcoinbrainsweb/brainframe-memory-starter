---
name: quitchat
description: >-
  Session-close orchestrator: scan conversation state, persist the handoff, run the
  memory audit, write a velocity row, then close the session. Use when the operator
  says "quitchat", "wrap up", "end session", or when exchange/context triggers fire.
---

# Quitchat orchestrator

Closes a working session in a fixed order and records what happened.

## Orchestration order (hard requirement)

```
scan -> save -> audit -> VELOCITY WRITE -> close
```

The substring `audit -> VELOCITY WRITE` is meant to appear verbatim in your
session-close checklists so a reviewer can grep for it and confirm the velocity write
runs after the audit (so any applied memory changes are reflected).

## Phase map

| Step | Action | Purpose |
|------|--------|---------|
| scan | classify the session, summarize exchanges, raise flags | know what happened |
| save | write the handoff / durable session artifacts | persist context |
| audit | run the memory-audit write gate over stored memory | keep memory lean |
| VELOCITY WRITE | insert a `velocity_sessions` row | record session metadata |
| close | retrospective, promotions, final hooks, close the row | end cleanly |

Under context pressure, run `scan -> save` only. Do not run the full
audit / velocity / close unless the operator escalates to a full quitchat.

## After save returns

1. Invoke the memory-audit routine.
2. It produces a per-entry verdict report (KEEP / TIGHTEN / MOVE_TO_FILE /
   MOVE_TO_STATE / REMOVE).
3. If memory is clean, continue straight to the velocity write.
4. If it raises flags, surface them inline and prompt `apply N,M / apply all / skip`.
   Apply only what the operator approves.
5. The audit never blocks quitchat. If the operator skips or does not respond, log
   "audit run, changes deferred" and continue.

## Velocity write

Insert one row summarizing the session's output:

```sql
INSERT INTO velocity_sessions (project, project_category, session_date, exchange_count, commits, loc_delta, weighted_score)
VALUES ('{project}', '{category}', current_date, {exchanges}, {commits}, {loc_delta}, {score});
```

## Close

Close the current session row. Status moves to `closed` (or `handed_off` if a
handoff chain is still open):

```sql
UPDATE sessions
   SET status = 'closed',
       ended_at = now(),
       summary = :summary,
       last_seen_at = now()
 WHERE id = '{current_session_id}';
```

## Credentials

Read your secrets from your secrets manager or `.env` at runtime, never inline them:
- a database service key for the memory-audit MOVE_TO_STATE applies and the velocity
  and close writes;
- a source-control token if your save/promotion steps write files.

See the root `.env.example` for the variable names.

## Failure modes

Never block quitchat on memory issues. If the audit cannot fetch a source, continue
with a partial audit and surface what was skipped. If an apply step errors mid-run,
stop the apply loop, surface what succeeded, and close anyway. Memory drift is
recoverable next session; a missed close artifact is not.
