> Status: runnable with your inputs
> The full runner now imports cleanly on the Python standard library alone (the third-party `httpx`, `pexpect`, `psutil`, and `psycopg2` are imported lazily, only where used); to actually build and verify you supply your own Postgres/Supabase, a GitHub PAT, model API keys, and a builder CLI. See Requirements below.

# Foreman system: SETUP

## What it does

Foreman is an automated spec build runner. Given one or more approved specs, it
dispatches a build agent, runs a cold verify agent from a different model family,
applies a database-invariant gate and a substance discriminator, and commits to the
target branch via a fast-forward-only merge, with no human in the loop. It keeps its
state in the database with a write-ahead state machine, so a run is always
recoverable after a crash.

## What Foreman does, step by step

Given approved specs, Foreman:

1. Resolves the bundle, validates the approval flags, and topologically sorts by
   `depends_on`.
2. Inserts a durable, claimable task queue in your database (survives a restart).
3. For each task in order:
   - checks database invariants through a trusted, credential-isolated harness
     before building (a pre-build gate);
   - confirms the actual model string from provider metadata, not the agent's
     self-report;
   - creates a feature branch and dispatches a build agent;
   - dispatches a cold verify agent from a different model family (mandatory);
   - on first fail, retries once with the verifier's findings injected;
   - on second fail, parks the spec with its failure trail and halts declared
     dependents;
   - on pass, runs the substance discriminator, then fast-forward-merges to the
     base ref and confirms the remote ref advanced.
4. Emits a consolidated run report (committed / parked / dependent-halted /
   excluded).

## Prerequisites you supply

- Your own Postgres database, or a Supabase project (any tier).
- A secrets manager or a local `.env` file. Foreman reads secrets by name; point
  `SECRETS_MANAGER_GET` (single secret) or `SECRETS_DOWNLOAD_CMD` (whole-blob JSON)
  at your provider's CLI, or supply the values directly as environment variables.
- Model API keys for whichever provider you use, for at least two different model
  families (one to build, one to verify).
- A GitHub personal access token with `repo` and `workflow` scope for the branch,
  push, and merge operations. Foreman resolves the push token from the environment
  first (`FOREMAN_SANDBOX_PUSH_TOKEN`, `GH_TOKEN`, `GITHUB_PRIMARY_PAT`) and falls
  back to your secrets manager.

## Steps

1. Apply the schema:
   ```
   psql "$DATABASE_URL" -f harness/foreman/SCHEMA.sql
   ```
   It creates `build_runs`, `build_run_specs`, `foreman_verification_receipts`, and
   the data-gathering tables (`foreman_gather_runs`, `foreman_gather_shards`).
2. Edit the project-slug defaults to your own: `project text NOT NULL DEFAULT
   'project_a'` on `build_runs`, and set the model defaults
   (`YOUR_VERIFY_MODEL`) to your real model strings.
3. In `scripts/models.py`, set `BUILDER_MODEL` and `VERIFIER_MODEL` to your two
   model strings. They must resolve to different families (the file enforces this).
4. Set the environment variables listed under "Foreman system" in the root
   `.env.example`.
5. Run the entrypoint. The package uses relative imports, so run it as a module
   from the repo root: `python -m harness.foreman.scripts.runner` (single-spec
   build-verify-commit loop) or `python -m harness.foreman.scripts.bundle_runner`
   (whole-bundle orchestration).

## Module map (what shipped here)

All modules live under `harness/foreman/scripts/` and import each other with
relative imports (`from .ledger import ...`).

```
harness/foreman/scripts/
  runner.py                            single-spec build-verify-commit loop
  bundle.py                            bundle intake, topo-sort, atomic commit
  bundle_runner.py                     bundle orchestration loop
  queue.py                             durable task queue: claim, stale-claim recovery, halt dependents
  report.py                            run report (committed/parked/dependent-halted/excluded)
  intake.py                            approval-flag check
  gates.py                             invariant-harness protocol, model precondition, substance gate
  ci_gate.py                           GitHub Check Runs poll/gate before merge
  ledger.py                            durable ledger: in-memory + Supabase REST backends
  reconcile.py                         zombie build_run sweep
  heartbeat.py                         per-task liveness heartbeat sink
  transport.py                         agent transport: PTY-CLI and in-process API adapters
  pty_harness.py                       drives the builder CLI in a PTY (pexpect)
  cli_session.py                       per-attempt CLI session: env scrub, worktrees, git identity
  watchdog.py                          liveness supervision + process-tree probes (psutil, lazy)
  tui_contract.py                      terminal screen-state matchers for the builder TUI
  output_schema.py                     structured-output schema registry + one-shot repair
  agent_tools.py                       tool schemas + in-process dispatcher
  sandbox.py                           optional Docker sandbox (--network none) for tool exec
  agent_harness.py                     the model API tool-use agent loop (httpx, lazy)
  foreman_db_harness.py                trusted DB-invariant check (credential isolated, psycopg2 lazy)
  foreman_substance_discriminator.py   coverage / mutation / negative-control substance methods
  substance_delta.py                   deliverable-delta substance gate
  antislop_lint.py                     deterministic anti-slop static lint over a diff
  manifest_lint.py                     pre-dispatch structural spec lint
  conformance.py                       mechanical conformance checklist gate
  live_db_assert.py                    live-DB verification via Supabase REST
  git_ops.py                           git operations (ls-remote, ff-merge, post-push verification)
  models.py                            status enum, valid transitions, config dataclasses, family check
  prompts.py                           prompt renderers for the build and verify agents
  worker_pool.py                       re-export of the subagents WorkerPool (single source of truth)
  loops/loop_005_test_coverage.py      one worked example build loop
```

## Design rules to preserve

- The build agent and the verify agent must be from different model families. A
  model must never grade its own family's output. `models.py:assert_different_family`
  refuses to start a run when the two share a family.
- The verify agent runs cold: it gets the diff and the spec, not the build agent's
  reasoning. Independence is the whole point of the check.
- The substance signal is mechanical. An LLM may explain a pass or fail but cannot
  overturn the mechanical verdict.
- State is write-ahead: every status transition is written to the database before
  the action it names begins, so an interrupted run resumes from the last durable
  state rather than redoing or skipping work.
- A two-way system needs a paired write-then-read smoke test before you trust it:
  enqueue one task, claim it, and read the row back before pointing Foreman at real
  specs.
- Secrets travel only in memory and in request headers, never in argv and never in
  logs. The redaction helper in `agent_harness.py` scrubs credential-like prefixes
  from any error body before it is stored.

## Requirements

Nothing is stubbed any more: the whole package imports on the standard library.
To make it do work you supply the following.

Environment variables (grouped under "Foreman system" in the root `.env.example`):

- `SUPABASE_URL`, `SUPABASE_SERVICE_KEY` -- your Postgres/Supabase project and
  service-role key; the `SupabaseLedger` and `live_db_assert` drive REST from these.
- `GITHUB_PRIMARY_PAT` (plus optional `FOREMAN_SANDBOX_PUSH_TOKEN` / `GH_TOKEN`) --
  a PAT with `repo` + `workflow` scope for branch/push/merge and the CI-gate poll.
- `FOREMAN_SPEC_REPO` (`YOUR_ORG/YOUR_SPEC_REPO`) and `FOREMAN_SPEC_REPO_PAT` --
  the repo and token `ledger.fetch_spec_body` reads spec markdown from.
- `BUILDER_CLI_CMD` -- the coding-agent CLI that writes code, driven in a PTY by
  `pty_harness.py` (defaults to the placeholder `your-builder-cli`).
- `FOREMAN_BUILDER_MODEL`, `FOREMAN_VERIFIER_MODEL` (or set `BUILDER_MODEL` /
  `VERIFIER_MODEL` in `models.py`) -- the two model strings; they must resolve to
  different families.
- `ANTHROPIC_API_KEY` -- the build/verify agent loop.

Optional third-party Python packages (import lazily; install only the paths you
use, none are needed just to import the package):

- `httpx` -- the in-process agent API loop in `agent_harness.py` / `transport.py`.
- `pexpect` -- driving the builder CLI in a real PTY (Unix) in `pty_harness.py`.
- `psutil` -- process-tree liveness probes in `watchdog.py`.
- `psycopg2` -- the direct-Postgres path in `foreman_db_harness.py`.

`ci_gate.py` takes the repo `owner`/`repo` from its caller and polls the standard
GitHub Check Runs API, so it needs no dedicated CI URL variable.

## What is intentionally not included

- Project-specific build loops were dropped; `loops/loop_005_test_coverage.py` ships
  as a single worked example.
- The invariant-registry example and a couple of thin caller shims named only in
  docstrings (for example `harness_caller`) were not extracted; the shipped set is a
  complete, importable package, not a turnkey deployment.
- GPG/DCO commit signing is env-gated (`FOREMAN_GPG_KEY_ID`) and off by default.
- All specific model names, internal decision ids, pull-request numbers, and commit
  hashes were removed. Choose your own models and tracking.
