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
5. Run the entrypoint. `scripts/runner.py` is the single-spec build-verify-commit
   loop; `scripts/bundle_runner.py` orchestrates a whole bundle.

## Module map (what shipped here)

```
scripts/foreman/
  runner.py                            single-spec build-verify-commit loop
  bundle.py                            bundle intake, topo-sort, atomic commit
  queue.py                             durable task queue: claim, stale-claim recovery, halt dependents
  bundle_runner.py                     bundle orchestration loop
  report.py                            run report (committed/parked/dependent-halted/excluded)
  gates.py                             invariant-harness protocol, model precondition, substance gate
  foreman_db_harness.py                trusted DB-invariant check (credential isolated)
  foreman_substance_discriminator.py   coverage / mutation / negative-control substance methods
  agent_harness.py                     the model API tool-use agent loop
  git_ops.py                           git operations (ls-remote, ff-merge, post-push verification)
  models.py                            status enum, valid transitions, config dataclasses, family check
  prompts.py                           prompt renderers for the build and verify agents
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

## What is intentionally not included

- Project-specific build loops were dropped; `loops/loop_005_test_coverage.py` ships
  as a single worked example.
- Some orchestration modules referenced in the original module map
  (`ledger.py`, `transport.py`, `harness_caller.py`, `intake.py`, and the invariant
  registry example) were not extracted; the shipped set is enough to read the core
  loop and its gates but is not a turnkey package. Some cross-module imports (for
  example a `substance_delta` module) will therefore dangle until you supply them.
- CI-gating, GPG/DCO signing, and heartbeat features were deferred in the original
  and are not included here.
- All specific model names, internal decision ids, pull-request numbers, and commit
  hashes were removed. Choose your own models and tracking.
