# Subagent layering

How to fan a task out across many sub-workers without exhausting the host, and how
to assemble their results safely. This is the design behind `worker_pool.py`.

## The problem

A parent agent that spawns N sub-workers to process N entities in parallel can run
the host out of memory two ways: too many sub-workers live at once, and too much
result data held in memory at once. Fixed-size batches ("run 10 at a time") solve
neither well: they under-use the host when workers are cheap and over-commit it
when workers are expensive, and they still tempt you to collect all results before
writing anything down.

## The rules

### 1. Host-budgeted concurrency cap, resolved at run start

Concurrency is derived from the host's memory budget, not chosen as a fixed number:

```
cap = floor(HOST_MEMORY_BUDGET_MB / SUBAGENT_EST_FOOTPRINT_MB)
      clamped to [1, SUBAGENT_MAX_CONCURRENCY]
```

The cap is read from live environment at the start of each run (see
`WorkerPool._resolve_cap`), so it reflects the host's current state rather than a
value baked in at deploy time. It is never a bare integer literal in the dispatch
loop.

### 2. Admit a new worker only after a live worker exits

The pool holds a semaphore initialized to `cap`. A worker acquires a slot before it
runs and releases it when it exits, so a new worker starts the moment an old one
finishes. This is bounded live concurrency, not fixed-size groups: a slow worker
never holds back the ones that could start behind it, and the live count never
exceeds `cap`.

### 3. Shard to disk before the worker exits

Each sub-worker writes its result to a run-scoped shard path before it returns. The
parent assembles the final result from landed shards after each wave, and never
holds all sub-worker results in memory at once. The shard ledger
(`subagent_gather_shards`, see SCHEMA.sql) tracks each shard's status and a
heartbeat, so a stalled shard can be re-dispatched.

### 4. Prefer low-memory sources

Inside each sub-worker, prefer structured or streaming sources over pulling whole
documents into memory: a structured data API over a full HTML page, ranged reads
over whole-file fetches, streaming parse over load-then-parse. Free each buffer
before fetching the next. A bulk fetch is a justified fallback; record why you
chose it.

## Injecting the rules into sub-workers

When the parent classifies a task as fan-out or bulk-data, it prepends the
memory-discipline preamble (`MEMORY_DISCIPLINE_PREAMBLE` in `worker_pool.py`) to the
sub-worker's prompt, so every sub-worker is told the same four rules. Classification
by task capability is the primary gate; a content check is only a backstop.

## Auditability

Every run appends a `PoolRunRecord` (run id, cap, budget, footprint, item count) to
the pool's run ledger before dispatch, so the concurrency decision is auditable
after the fact. Persist the same fields to `subagent_gather_runs` if you want the
audit trail in the database.
