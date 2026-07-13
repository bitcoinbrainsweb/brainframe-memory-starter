# Council system: SETUP

## What it does

The council reviews a spec with N independent critics, each ideally from a
different model family, then synthesizes their findings. It runs two paths, a
parallel path where all critics review at once and a sequential path where a few
critics review in a chain, judges each path, merges the two, and finally has a
post-run reviewer audit the council process itself. Every critic output is
validated against a JSON schema, retried on malformed output up to a cap, then
marked DROPPED, and the whole run is recorded in a durable, concurrency-capped work
queue.

## Prerequisites you supply

- Model API keys for several providers. Seat 1 is an Anthropic model
  (`ANTHROPIC_API_KEY`); the other seats are any OpenAI-compatible endpoints you
  configure (`CRITIC_2_BASE_URL` / `CRITIC_2_MODEL` / `CRITIC_2_API_KEY`, and so
  on). Use different families so the council is genuinely diverse.
- Python packages `httpx` and (optionally) `jsonschema` for schema validation.
- Optionally, a Postgres/Supabase project for the `critique_runs` telemetry and
  queue. Without it the runner still works; the telemetry writer no-ops.

## Steps

1. Apply the schema:
   ```
   psql "$DATABASE_URL" -f harness/council/SCHEMA.sql
   ```
   It creates `critique_runs` and the claim/append/reclaim RPCs. Edit the caller
   ids in `chk_critique_runs_caller` to your own.
2. Set the environment variables under "Council system" in the root `.env.example`,
   including per-seat model, key, and base-URL variables.
3. Configure the seats. Edit `CRITIC_SEATS` in `run_council.py` to list the critics
   you want; keep them diverse across families.
4. Run it against a spec file:
   ```
   python harness/council/run_council.py path/to/spec.md --project project_a
   ```
   The output prints the parallel judge, the sequential judge, and the post-run
   review, and (if configured) writes a `critique_runs` row.

## Design rules to preserve

- The critics must be independent and cold. Each reviews the same spec without
  seeing the others' output on the parallel path. That independence is why a
  council catches what a single review misses.
- Use N critics from different model families. A council of five instances of the
  same model shares that model's blind spots and defeats the purpose.
- Validate every critic output against `critic.schema.json`. A malformed output is
  retried up to the cap, then marked DROPPED. Only DROPPED counts as a degraded
  seat, so an ABSTAIN or a gated seat does not falsely trip the "council degraded"
  alarm.
- The post-run reviewer is blinded: it sees findings tagged path-A / path-B / both
  without knowing which architecture produced which, so it audits the process
  rather than rubber-stamping it.
- The queue enforces a hard concurrency cap in one place (`claim_next_critique_run`
  with an advisory lock and SKIP LOCKED). Do not claim runs by ad-hoc UPDATE, or
  the cap and reclaim logic drift.
- For regulated content, gate the seats you have not cleared for that data class
  rather than dispatching to them. See `REGULATED_TRIGGERS` and
  `REGULATED_CLEARED_SEATS`.

## What is intentionally not included

- The original runner wired five named vendors directly and pulled in several
  optional sibling modules (reasoning-trace capture and embedding, retrieval,
  reasoning-aware dedupe, native-citations grounding, and per-critic process
  isolation). Those modules were not extracted. The runner keeps the same
  graceful-degradation pattern: when an optional capability is absent, the run
  continues without it.
- The five vendor-specific caller functions are collapsed into one
  `call_openai_compatible` plus the Anthropic caller, since they differed only by
  base URL and model string. Point each seat at whichever vendor you use.
- All specific model names, vendor names, cost figures, and internal ids were
  removed. Choose your own models and seats.
