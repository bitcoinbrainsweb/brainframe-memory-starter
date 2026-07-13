# harness/

Real, working, sanitized reference code for five database-backed agent systems.
This extends the file-based memory starter (in the repo root) with the systems that
need a database behind them: a queryable memory audit trail, session tracking, an
automated build/verify runner, a multi-critic review council, and a host-budgeted
subagent fan-out. Each system directory is self-contained: the extracted code, a
`SCHEMA.sql` where a database is involved, and a `SETUP.md` telling you exactly what
you must supply to run it.

This is reference-runnable, not turnkey. The logic and schema are real; the
credentials, model strings, and project slugs are placeholders. Nothing boots until
you bring your own database and keys. Start from the root `.env.example`, which lists
every placeholder grouped by system.

## The five systems

| System | One line | Setup |
|---|---|---|
| memory | An append-only `state_events` audit trail plus a memory-audit routine that keeps stored memory lean and failsafe-only. | [memory/SETUP.md](memory/SETUP.md) |
| sessions | Session tracking with an idle-not-closed model and a handoff chain, plus handchat / pickup / quitchat skills. | [sessions/SETUP.md](sessions/SETUP.md) |
| foreman | An automated spec build runner: build agent, cold verify agent from a different model family, DB-invariant gate, substance discriminator, ff-merge. | [foreman/SETUP.md](foreman/SETUP.md) |
| council | N independent critics from different model families review a spec on two paths, judged, merged, and audited by a blinded post-run reviewer. | [council/SETUP.md](council/SETUP.md) |
| subagents | Fan a task out across sub-workers with a host-memory-budgeted concurrency cap, admit-after-exit scheduling, and shard-to-disk assembly. | [subagents/SETUP.md](subagents/SETUP.md) |

Each `SETUP.md` opens with a runnable-vs-reference status line, so you know up front whether a system boots as-is or needs your inputs; a `reference-only` system ships the real logic but omits some internal sibling modules by design, and lists exactly which ones under its What is stubbed section.

## A few rules run through all of them

- The agent that builds and the agent that verifies must be different model
  families, and a review council must use several independent, cold critics. A model
  must not grade its own family's work.
- A two-way (write-then-read) system needs a paired smoke test before you trust it:
  write one row, read it back, confirm the round-trip.
- Session rows go idle rather than closing, so current state is inferred from
  `last_seen_at` and `status`, not from an explicit close.
- Secrets live in memory and request headers only, never in argv or logs, and are
  fetched by name from your secrets manager or the environment.

## Getting started

1. Read the root `.env.example` and fill in your own database URL, keys, and model
   strings.
2. Pick a system, open its `SETUP.md`, apply its `SCHEMA.sql`, and edit the
   project-slug CHECK constraints to your own.
3. Run its entrypoint.
