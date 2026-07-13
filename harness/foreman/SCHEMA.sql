-- Foreman system: build-loop, verification-receipt, and data-gathering tables.
-- Apply this to your own Postgres / Supabase project.
--
-- Foreman runs a bundle of specs through a build-then-verify loop with a
-- write-ahead state machine (every state transition is written to the DB before
-- the action it names begins, so a crash is always recoverable). These tables are
-- that durable state.

-- ---------------------------------------------------------------------------
-- Build-loop tables
-- ---------------------------------------------------------------------------

-- One row per bundle execution.
CREATE TABLE IF NOT EXISTS build_runs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  status text NOT NULL DEFAULT 'queued' CHECK (status IN (
    'queued', 'running', 'completed', 'failed', 'cancelled'
  )),
  bundle jsonb NOT NULL,
  ordered_bundle jsonb,
  session_id uuid,
  started_at timestamptz,
  completed_at timestamptz,
  report jsonb,
  -- human-readable run id + owning project
  run_id text UNIQUE,
  project text NOT NULL DEFAULT 'project_a',  -- replace with your own project slug
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

-- Per-spec state within a run.
-- Write-ahead: every transition is written before the corresponding action begins.
CREATE TABLE IF NOT EXISTS build_run_specs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id uuid NOT NULL REFERENCES build_runs(id),
  spec_slug text NOT NULL,
  build_order int,
  depends_on jsonb,
  status text NOT NULL DEFAULT 'queued' CHECK (status IN (
    'queued', 'building', 'verifying', 'merging', 'committed',
    'parked', 'dependent-halted', 'merge-conflict', 'merge-conflict-repeated'
  )),
  build_branch text,
  build_commit_sha text,
  builder_model text,
  verifier_model text,
  verify_result text,
  verify_report jsonb,
  retry_count int NOT NULL DEFAULT 0,
  failure_trail jsonb,
  idempotency_key uuid NOT NULL DEFAULT gen_random_uuid() UNIQUE,
  heartbeat_at timestamptz,
  -- parallel v2 columns (legacy names above are preserved for compatibility)
  position int,
  attempt int NOT NULL DEFAULT 0,
  branch_name text,
  base_sha text,
  commit_sha text,
  pr_id text,
  verifier_findings jsonb,
  park_reason text,
  spec_idempotency_key text UNIQUE,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(run_id, spec_slug)
);

CREATE INDEX IF NOT EXISTS idx_build_run_specs_run_id ON build_run_specs(run_id);
CREATE INDEX IF NOT EXISTS idx_build_run_specs_status ON build_run_specs(status);
CREATE INDEX IF NOT EXISTS idx_build_run_specs_heartbeat ON build_run_specs(heartbeat_at);

-- ---------------------------------------------------------------------------
-- Verification receipts
-- One row per verify pass over a pull request / diff.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS foreman_verification_receipts (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  pr_ref              text NOT NULL,
  spec_slug           text,
  spec_linked         boolean NOT NULL DEFAULT false,
  spec_fetch_error    boolean NOT NULL DEFAULT false,
  diff_sha            text,
  verdict             text NOT NULL CHECK (verdict IN ('PASS','NEEDS-INPUT','ERROR')),
  findings            jsonb NOT NULL DEFAULT '[]',
  ac_count            integer,
  pass_count          integer,
  needs_input_count   integer,
  model               text NOT NULL DEFAULT 'YOUR_VERIFY_MODEL',  -- the verify-phase model string
  session_id          uuid,
  triggered_by        text NOT NULL DEFAULT 'manual',
  created_at          timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE foreman_verification_receipts ENABLE ROW LEVEL SECURITY;

-- ---------------------------------------------------------------------------
-- Data-gathering (fan-out) tables
-- A gather run fans out over N entities; each entity is one shard fetched and
-- extracted independently, then reduced. See the subagents system for the
-- host-budgeted concurrency model that drives the fan-out.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.foreman_gather_runs (
  gather_run_id           text        PRIMARY KEY,
  objective               text        NOT NULL,
  entity_count            int         NOT NULL,
  shard_schema            jsonb       NOT NULL,
  reduce_model            text        NOT NULL,   -- the model string used for the reduce step
  synthesis_artifact_path text        NULL,
  started_at              timestamptz NOT NULL DEFAULT now(),
  ended_at                timestamptz NULL,
  session_id              text        NOT NULL,
  idempotency_key         text        NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS public.foreman_gather_shards (
  gather_run_id       text        NOT NULL REFERENCES foreman_gather_runs(gather_run_id),
  entity_key          text        NOT NULL,
  source_descriptor   jsonb       NOT NULL,
  required_for_synthesis bool     NOT NULL DEFAULT false,
  source_path         text        NULL,
  shard_path          text        NULL,
  status              text        NOT NULL DEFAULT 'queued'
                      CHECK (status IN (
                        'queued','fetching','fetched','extracting','extracted',
                        'fetch-failed','extract-failed','complete','partial','absent'
                      )),
  missing_fields      jsonb       NULL,
  repull_attempts     int         NOT NULL DEFAULT 0,
  last_heartbeat_at   timestamptz NOT NULL DEFAULT now(),
  failure_reason      text        NULL,
  PRIMARY KEY (gather_run_id, entity_key)
);

CREATE INDEX IF NOT EXISTS idx_foreman_gather_shards_status
  ON public.foreman_gather_shards (gather_run_id, status);

CREATE INDEX IF NOT EXISTS idx_foreman_gather_shards_heartbeat
  ON public.foreman_gather_shards (last_heartbeat_at);
