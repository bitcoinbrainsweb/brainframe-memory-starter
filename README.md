# memory-starter

This repo has two tiers.

The original starter is the light version: a file-based memory system for Claude Code, plain markdown and skills, no database and no accounts. It installs in minutes and covers a single operator on a single machine. The harness/ tree is the full version: the database-backed systems for running AI agents against real work, where state must survive restarts, be queryable, and leave an audit trail. The light version is for trying the ideas; the full version is for depending on them.

The harness ships five systems, each with real extracted code, its schema, and a SETUP.md stating what you must supply:

- **memory**: an append-only event log with typed rules and a drift audit. Records what agents decided and changed, immutably, so current state is always reconstructable and auditable.
- **sessions**: a session registry with handoff chains. Work survives chat and context boundaries: one session writes a brief and a pickup slug, the next claims it cold, and a collision guard stops two live sessions from working the same thread.
- **foreman**: a build-verify-fix orchestrator. A queue and run ledger dispatch specs to a builder CLI; deterministic gates check the output. A manifest lint refuses structurally incomplete specs before any tokens are spent, a static lint rejects slop patterns in diffs, a substance gate fails changes that deliver scaffolding instead of the deliverable, a CI gate polls checks, and heartbeat plus reconcile catch runs that die mid-build. Each gate blocks a failure mode that judgment-based review lets through.
- **council**: multi-model spec critique. Independent critics review a spec cold, in parallel and in sequence, judges consolidate, and a blinded reviewer audits the run itself. Catches the blind spots a single author or single model misses.
- **subagents**: a bounded worker pool with a memory-budget cap and a fan-out ledger, for running many agent tasks concurrently without exhausting the host.

Start with harness/quickstart. Four demos run the real code paths on local storage with canned model responses: no accounts, no keys, under five minutes. The per-system SETUP.md files are the real path and state exactly what each system needs: your own Postgres or Supabase, model API keys, and for foreman a builder CLI.

---

A lightweight AI memory system for Claude Projects. Fork this repo, run one script, paste instructions into Claude — done.

**What you get:** Claude that remembers across sessions. Decisions logged. Notes searchable. Works with Claude Pro (web) or Claude Code (CLI).

**Stack:** GitHub (canonical store) + Supabase free tier (queryable store) + Claude Project instructions (boot surface).

---

## Quickstart

1. Click **Use this template** → create your own repo (e.g. `my-memory`)
2. Clone it locally
3. Run `bash scripts/onboard.sh`
4. Paste contents of `SYSTEM/PROJECT_INSTRUCTIONS_OWNER.md` into a new Claude Project
5. Done. Open Claude and type `boot`.

Full guide: [docs/agent-guides/onboarding.md](docs/agent-guides/onboarding.md)

---

## File Layout

```
CLAUDE.md                           Boot file — paste into Claude Project instructions
SYSTEM/                             Template-managed. Never edit manually.
  GLOBAL_RULES.md                   Claude's universal rules
  PROJECT_INSTRUCTIONS_OWNER.md     Paste into your Claude Project
  PROJECT_INSTRUCTIONS_CONTRIBUTOR.md
  PROJECT_INSTRUCTIONS_READER.md
USER/                               Yours. Customize freely. Never overwritten by upstream.
  routing/
    facts.md                        Immutable facts about your context
    preferences.md                  How you like Claude to behave
    decisions.md                    Append-only decision log
    sessions.md                     Rolling last-5 session summaries
  people.md                         Collaborators, clients, contacts
  topics/                           Per-topic folders
contributions/                      Inbox — contributors write here, owner promotes
.claude/
  settings.json                     Claude Code deny rules (Claude Code users only)
  skills/                           Skill stubs
scripts/
  onboard.sh                        One-command setup
  migrations/
    001-initial-schema.sql          Supabase schema — idempotent
docs/
  agent-guides/
    memory-architecture.md
    onboarding.md
    maintenance.md
```

---

## Roles

| Role | Can do |
|------|--------|
| Owner | Read + write everything. Promotes contributions. |
| Contributor | Writes to `contributions/` inbox only. |
| Reader | Read-only queries via Claude. |

Solo? You are the Owner. Skip Contributor and Reader.

---

## System vs User files

**Never edit `SYSTEM/`.** It receives upstream updates as PRs you review and merge. Your content lives in `USER/`, `contributions/`, and your topics.

See [Receiving Updates](docs/agent-guides/onboarding.md#receiving-updates).

---

## Requirements

- GitHub account + basic git
- Supabase account (free tier)
- Claude Pro or Team plan
- bash + curl (for onboarding script)

Windows users: run via WSL or follow the manual steps in [docs/agent-guides/onboarding.md](docs/agent-guides/onboarding.md).

---

## Surface

Claude Code (CLI): full read/write via curl to Supabase REST API. Credentials stored at `~/.config/memory-starter/.env`.

claude.ai Projects (web/mobile): Claude produces SQL or curl commands for you to run. No direct HTTP from the browser session.

---

A lightweight memory architecture for Claude — battle-tested patterns, open for anyone to use.
