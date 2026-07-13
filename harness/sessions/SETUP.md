> Status: runnable with your inputs
> This system ships a schema plus agent skills rather than a Python entrypoint; apply `SCHEMA.sql` to your own Postgres/Supabase, edit the example slugs, and install the skills to use it (SQL not boot-tested here, no local Postgres).

# Sessions system: SETUP

## What it does

This tracks each working conversation as a `sessions` row and links related
conversations into a handoff chain, so a long thread of work spanning many chats is
one traceable chain of rows. Sessions go idle rather than closing: a row keeps its
`last_seen_at` and `status`, and current state is inferred from those. An optional
`velocity_sessions` table measures output per session (exchanges, commits, lines
changed, a weighted score).

## Prerequisites you supply

- Your own Postgres database, or a Supabase project (any tier).
- A secrets manager or a local `.env` file (see the root `.env.example`).
- An agent runtime that runs the session skills (`handchat`, `pickup`, `quitchat`).

## Steps

1. Apply the schema:
   ```
   psql "$DATABASE_URL" -f harness/sessions/SCHEMA.sql
   ```
   It creates the `sessions` table (with the handoff-chain self-reference, RLS, and
   indexes) and, optionally, the `velocity_sessions` table.
2. Edit the CHECK constraints to your own slugs: replace `('project_a','project_b')`
   in `chk_sessions_project`, and `('category_a','category_b')` in the velocity
   `project_category` check.
3. Set the environment variables under "Sessions system" in the root `.env.example`.
4. Install the skills in `skills/` into your agent so it can start, pause, resume,
   and close sessions. `handchat` pauses mid-session, `pickup` resumes in a new chat,
   `quitchat` closes and records velocity.

## Design rules to preserve

- Session rows go idle rather than closing. `status` stays `active` until an explicit
  close, and `last_seen_at` on active rows is how you find idle sessions. Do not
  assume a missing recent write means "closed": infer status, do not overwrite it.
- The handoff chain is a self-reference: a chain head points its `handoff_chain_id`
  at its own id (that is why the foreign key is DEFERRABLE), and each continuation
  inherits the head's chain id. Reconstruct a whole thread with one query on
  `handoff_chain_id`.
- `pickup_slug` is unique per project among sessions that still hold one; the partial
  unique index enforces the collision guard that handchat checks for.
- Writes to the current row use optimistic locking on `updated_at`. On a lost race,
  record a `state_events` conflict row and retry once; never silent-drop a handoff.
- A two-way system needs a paired write-then-read smoke test: run handchat to write a
  `context_brief` and `pickup_slug`, then run pickup to read them back, before you
  trust the pair in real sessions.

## What is intentionally not included

- The `handoff` capability is a schema feature (the `handoff_chain_id` self-reference),
  not a separate skill; there was no standalone handoff skill to extract. The chain
  logic lives in `handchat` (chain resolution) and `pickup` (chain inheritance).
- The velocity write in `quitchat` is shown as a single representative INSERT; the
  internal scoring rubric that computes `weighted_score` is project-specific and not
  included. Choose your own weights.
- Internal orchestration plumbing (promotion logs, retrospective quizzes, the
  provenance columns on state_events) was dropped. The skills keep the DB mechanics
  (optimistic-lock UPDATE, unique pickup_slug, chain trace, close transition).
