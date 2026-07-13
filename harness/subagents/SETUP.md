# Subagents system: SETUP

## What it does

This is how a parent agent fans a task out across many sub-workers without
exhausting the host. It derives its concurrency cap from the host's memory budget at
run start, admits a new sub-worker only after a live one exits, and has each
sub-worker write its result to disk before returning so the parent never holds every
result in memory at once. A run and its shards are tracked in a durable ledger with
per-shard heartbeats, so a stalled shard can be re-dispatched.

## Prerequisites you supply

- Python 3.10+ (the pool uses only the standard library).
- Values for the three budget environment variables (see below).
- Optionally, your own Postgres/Supabase project if you want the run and shard
  ledger persisted.

## Steps

1. Set the three budget variables (listed under "Subagents system" in the root
   `.env.example`):
   ```
   export HOST_MEMORY_BUDGET_MB=8192       # memory you will let the fan-out use
   export SUBAGENT_EST_FOOTPRINT_MB=512    # estimated peak memory per sub-worker
   export SUBAGENT_MAX_CONCURRENCY=16      # hard ceiling regardless of the budget math
   ```
   With these, the cap is `floor(8192 / 512) = 16`, clamped to 16.
2. Use `WorkerPool` from `worker_pool.py`:
   ```python
   from worker_pool import WorkerPool
   pool = WorkerPool()
   results = pool.run("run-123", items, worker_fn)
   ```
   The pool resolves the cap from live env on every `run()` call.
3. Optionally apply the ledger schema so a fan-out's runs and shards are durable:
   ```
   psql "$DATABASE_URL" -f harness/subagents/SCHEMA.sql
   ```
4. Read `layering.md` for the four design rules and inject
   `MEMORY_DISCIPLINE_PREAMBLE` into your sub-workers' prompts at the fan-out
   boundary.

## Design rules to preserve

- Concurrency is derived from the host memory budget at run start, never a fixed
  literal. Read it from live env so it tracks the host, not a deploy-time snapshot.
- Admit a new sub-worker only after a live one exits (a semaphore, not fixed
  batches), so the live count never exceeds the cap and a slow worker does not stall
  the queue behind it.
- Each sub-worker writes its result to a shard on disk before returning; the parent
  assembles from landed shards after each wave. Never hold every sub-worker result
  in memory at once.
- Inside a sub-worker, prefer structured or streaming sources over whole-document
  fetches, and free each buffer before fetching the next.
- Record the cap, budget, and footprint for every run so the concurrency decision is
  auditable after the fact.

## What is intentionally not included

- There was no standalone subagent budget/concurrency database migration in the
  source; the concurrency logic is code (`worker_pool.py`) and the durable state is
  the shard ledger in `SCHEMA.sql`, which is the same fan-out persistence the
  foreman system uses. Installing either system's copy of those tables is enough.
- The pool ships as a threading-based reference implementation. Swap in your own
  executor (processes, a job queue, remote workers) as long as you keep the
  budget-derived cap and the admit-after-exit rule.
