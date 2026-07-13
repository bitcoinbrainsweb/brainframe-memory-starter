-- Subagents system: fan-out run + shard ledger.
-- Apply this to your own Postgres / Supabase project.
--
-- NOTE ON SOURCE: the source repos had no standalone "subagent budget/concurrency"
-- migration. The subagent concurrency/budget logic lives in code (see
-- worker_pool.py in this directory), and the DURABLE state of a subagent fan-out is
-- this run + shard ledger, which is the same schema the build system's
-- data-gathering mode uses. One fan-out is one run; each subagent handles one
-- shard, which it fetches, extracts, and lands independently. The parent assembles
-- results from landed shards after each wave rather than holding them all in
-- memory (see the shard-to-disk rule in layering.md). These tables are also present
-- in the foreman system's SCHEMA.sql; installing either one is enough.

CREATE TABLE IF NOT EXISTS public.subagent_gather_runs (
  gather_run_id           text        PRIMARY KEY,
  objective               text        NOT NULL,
  entity_count            int         NOT NULL,     -- how many shards (subagents) this run fans out to
  shard_schema            jsonb       NOT NULL,     -- the per-shard result contract
  reduce_model            text        NOT NULL,     -- the model string used for the reduce/synthesis step
  synthesis_artifact_path text        NULL,
  started_at              timestamptz NOT NULL DEFAULT now(),
  ended_at                timestamptz NULL,
  session_id              text        NOT NULL,
  idempotency_key         text        NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS public.subagent_gather_shards (
  gather_run_id       text        NOT NULL REFERENCES subagent_gather_runs(gather_run_id),
  entity_key          text        NOT NULL,          -- which entity this subagent is responsible for
  source_descriptor   jsonb       NOT NULL,
  required_for_synthesis bool     NOT NULL DEFAULT false,
  source_path         text        NULL,
  shard_path          text        NULL,              -- where the subagent landed its result on disk
  status              text        NOT NULL DEFAULT 'queued'
                      CHECK (status IN (
                        'queued','fetching','fetched','extracting','extracted',
                        'fetch-failed','extract-failed','complete','partial','absent'
                      )),
  missing_fields      jsonb       NULL,
  repull_attempts     int         NOT NULL DEFAULT 0,
  last_heartbeat_at   timestamptz NOT NULL DEFAULT now(),  -- liveness; a stalled shard is re-dispatched
  failure_reason      text        NULL,
  PRIMARY KEY (gather_run_id, entity_key)
);

CREATE INDEX IF NOT EXISTS idx_subagent_gather_shards_status
  ON public.subagent_gather_shards (gather_run_id, status);

CREATE INDEX IF NOT EXISTS idx_subagent_gather_shards_heartbeat
  ON public.subagent_gather_shards (last_heartbeat_at);
