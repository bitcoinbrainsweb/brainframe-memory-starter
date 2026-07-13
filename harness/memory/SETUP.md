> Status: runnable with your inputs
> This system ships a schema and an audit routine rather than a Python entrypoint; apply `SCHEMA.sql` to your own Postgres/Supabase and edit the example slugs to use it (SQL not boot-tested here, no local Postgres).

# Memory system: SETUP

## What it does

This is the database-backed half of the memory starter. Where the file-based
starter keeps notes and decisions in Markdown, this adds an append-only event log
(`state_events`) so every change to a tracked entity is recorded with its actor,
its type, and a full before/after JSONB snapshot. On top of that log sits an audit
routine that keeps stored memory entries lean, non-duplicated, and failsafe-worthy.

## Prerequisites you supply

- Your own Postgres database, or a Supabase project (any tier).
- A secrets manager or a local `.env` file for credentials (see the root
  `.env.example`). Never commit real values.
- If you run the audit routine against a live memory store, an API key for
  whichever model provider you use to score entries.

## Steps

1. Apply the schema:
   ```
   psql "$DATABASE_URL" -f harness/memory/SCHEMA.sql
   ```
   or paste `SCHEMA.sql` into the Supabase SQL editor. It installs the `pg_trgm`
   extension, the shared `update_updated_at_column()` helper, and the
   `state_events` table with row-level security on.
2. Set the environment variables listed under "Memory system" in the root
   `.env.example` (at minimum your database URL and service key).
3. Copy `rules/global.yaml` and `rules/example.yaml` and edit them: replace the
   example project slugs, the token prefixes, and the name allowlist with your own.
   Rename `example.yaml` to match your project.
4. Use `audit/memory-audit.md` as the routine your agent follows to audit memory.
   It writes an append-only log; `audit/audit-log.template.md` shows the format.

## Design rules to preserve

- Memory is failsafe-only. If a fact fits better as a database row or a doc, move
  it there. The Gate 5 check in the audit routine exists to enforce this.
- The event log is append-only. Never update or delete a `state_events` row; write
  a new event that supersedes it. That is what makes history trustworthy.
- Two-way I/O systems need a paired write-then-read smoke test before you trust
  them: insert one event, read it back by `entity_id`, and confirm the JSONB
  round-trips before wiring anything to this table.
- Apply audit verdicts by content hash, never by line number, and re-resolve the
  target immediately before each apply. Memory can change between snapshot and
  apply; hashing is what keeps the apply race-safe.
- No memory entry is ever removed before its target write (a file append or a row
  insert) has succeeded.

## What is intentionally not included

- No real memory snapshots or health reports. `audit/` ships a template and a
  documented routine only.
- The project-specific rule bodies were removed. `rules/example.yaml` keeps one
  representative rule of each supported kind so you can see the shape.
- The `session_id` foreign key on `state_events` is left as a plain column so this
  schema installs standalone. Add the constraint yourself if you also install the
  sessions system.
