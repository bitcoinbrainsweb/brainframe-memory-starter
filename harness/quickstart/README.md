# Quickstart: see it work in five minutes, no accounts

Each system in `harness/` has a `SETUP.md` whose first real step is "bring your own
Postgres/Supabase and model API keys." That is the real path. This directory is the
short path: four self-contained demos that show each system's real code actually
running, with **no accounts, no network, no API keys, and no install** beyond
Python 3 (the standard library only, `sqlite3` included).

Where a demo would otherwise need a database or a model API, it uses local storage
(a throwaway SQLite file) or canned, obviously-synthetic responses from
`fixtures/`. The system's own logic is real; only the infrastructure is stubbed.

## Run them

```
python3 harness/quickstart/demo_memory.py
python3 harness/quickstart/demo_sessions.py
python3 harness/quickstart/demo_council.py
python3 harness/quickstart/demo_foreman_gates.py
```

Each prints a narrated transcript and exits 0.

## What each one shows

| Demo | Shows | Real vs stubbed |
|---|---|---|
| `demo_memory.py` | The append-only `state_events` audit log: three events written, then read back as a log and reconstructed into one entity's history. | Real event-log shape and append-only discipline; SQLite (a portable subset of `harness/memory/SCHEMA.sql`) stands in for Postgres/JSONB. |
| `demo_sessions.py` | The session handoff chain and pickup state machine: open session A, write a handoff brief and pickup slug, hand off, then open session B that claims the slug and inherits the chain. | Real state machine, real pickup-slug collision guard (a partial unique index), real idle-not-closed transitions; SQLite stands in for Postgres. |
| `demo_council.py` | The real multi-critic council pipeline: parallel critics, a sequential chain, two judges, and a blinded post-run reviewer, ending in merged findings. | Real phase flow from the shipped `run_council.py` (seat dispatch, retry/validation, degraded-seat accounting, two-judge gate); only the model responses are canned fixtures. |
| `demo_foreman_gates.py` | Foreman's three deterministic pre/post-build gates run on toy fixtures: `manifest_lint` (spec completeness), `antislop_lint` (placeholder/junk code), `substance_delta` (real deliverable vs empty calories). | Fully real: these gates are pure and offline by design. The demo imports the shipped modules and feeds them toy specs and diffs. |

## Honest limits

These are demonstrations of the real code paths with local storage and canned model
responses. They are not the product. For real use -- your own data, your own model
families, durable Postgres/Supabase storage, a real builder CLI -- follow each
system's `SETUP.md`. The quickstart does not change what any system requires for
real use; it only lowers the bar to *see* the code run.

The council demo wires its canned responses through `run_council.py`'s existing call
seams (`call_anthropic` / `call_openai_compatible`) by replacing those module
attributes from the demo. No shipped system code was modified to enable the
quickstart.
