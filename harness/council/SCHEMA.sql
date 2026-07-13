-- Council system: critique_runs table + queue/claim RPCs.
-- Apply this to your own Postgres / Supabase project.
--
-- A council run is one review of one spec by N critics. This table is the durable
-- record and work queue: a run is inserted 'queued', claimed 'running' (with a
-- strict concurrency cap), and finalized 'complete' / 'partial' / 'failed'. The
-- RPCs below are the only supported way to claim and update runs, so concurrency
-- and reclaim logic live in one place.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS public.critique_runs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  spec_path TEXT NOT NULL,
  spec_project TEXT NOT NULL,
  spec_sha TEXT NOT NULL,
  data_class TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued',
  critics_requested TEXT[] NOT NULL,
  critics_succeeded TEXT[] NOT NULL DEFAULT '{}'::text[],
  critics_failed TEXT[] NOT NULL DEFAULT '{}'::text[],
  critics_skipped TEXT[] NOT NULL DEFAULT '{}'::text[],
  skip_reason JSONB NOT NULL DEFAULT '{}'::jsonb,
  vendors_used TEXT[] NOT NULL DEFAULT '{}'::text[],
  retrieval_used BOOLEAN NOT NULL DEFAULT false,
  raw_outputs JSONB NOT NULL DEFAULT '{}'::jsonb,
  judge_vendor TEXT,
  judge_model TEXT,
  judge_output TEXT,
  verdict JSONB,
  total_cost_usd NUMERIC(10,4),
  idempotency_key TEXT NOT NULL,
  queued_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  started_at TIMESTAMPTZ,
  completed_at TIMESTAMPTZ,
  duration_ms INTEGER,
  error_message TEXT,
  caller TEXT,
  caller_override_reason TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at TIMESTAMPTZ GENERATED ALWAYS AS (created_at + interval '90 days') STORED,
  CONSTRAINT chk_critique_runs_data_class
    CHECK (data_class IN ('public','confidential','regulated')),
  CONSTRAINT chk_critique_runs_status
    CHECK (status IN ('queued','running','complete','partial','failed')),
  CONSTRAINT chk_critique_runs_caller
    CHECK (caller IS NULL OR caller IN ('agent','spec_writing','ci','manual')),  -- replace with your own caller ids
  CONSTRAINT uq_critique_runs_idempotency
    UNIQUE (spec_path, spec_sha, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_critique_runs_status_queued_at
  ON public.critique_runs (queued_at)
  WHERE status = 'queued';

CREATE INDEX IF NOT EXISTS idx_critique_runs_spec_path_created_at
  ON public.critique_runs (spec_path, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_critique_runs_spec_project_created_at
  ON public.critique_runs (spec_project, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_critique_runs_running
  ON public.critique_runs (status, started_at)
  WHERE status = 'running';

CREATE OR REPLACE FUNCTION public.set_updated_at()
RETURNS TRIGGER AS $$
BEGIN NEW.updated_at = now(); RETURN NEW; END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_critique_runs_updated_at ON public.critique_runs;
CREATE TRIGGER trg_critique_runs_updated_at
  BEFORE UPDATE ON public.critique_runs
  FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

-- RLS: enable but no policies. The service role bypasses; all other access is blocked.
ALTER TABLE public.critique_runs ENABLE ROW LEVEL SECURITY;

-- ---------------------------------------------------------------------------
-- RPCs. All SECURITY DEFINER, granted to service_role only. These are the only
-- supported way to claim and update runs, so concurrency and reclaim logic is not
-- duplicated across callers.
-- ---------------------------------------------------------------------------

-- 1. Claim the next queued run (strict 2-concurrency via advisory lock + SKIP LOCKED).
--    Returns the claimed row, or nothing if the queue is empty or the cap is reached.
CREATE OR REPLACE FUNCTION public.claim_next_critique_run()
RETURNS SETOF public.critique_runs
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  running_count INT;
  claimed_id UUID;
BEGIN
  PERFORM pg_advisory_xact_lock(hashtext('critique_slot_claim'));

  SELECT COUNT(*) INTO running_count
    FROM public.critique_runs
   WHERE status = 'running';

  IF running_count >= 2 THEN
    RETURN;
  END IF;

  SELECT id INTO claimed_id
    FROM public.critique_runs
   WHERE status = 'queued'
   ORDER BY queued_at
   FOR UPDATE SKIP LOCKED
   LIMIT 1;

  IF claimed_id IS NULL THEN
    RETURN;
  END IF;

  RETURN QUERY
    UPDATE public.critique_runs
       SET status = 'running', started_at = now()
     WHERE id = claimed_id
     RETURNING *;
END;
$$;

GRANT EXECUTE ON FUNCTION public.claim_next_critique_run() TO service_role;
REVOKE EXECUTE ON FUNCTION public.claim_next_critique_run() FROM PUBLIC, anon, authenticated;

-- 2. Append a critic result to an existing run (success path).
--    Uses the jsonb || operator so prior critics' outputs are preserved.
CREATE OR REPLACE FUNCTION public.append_critic_success(
  p_run_id UUID,
  p_critic_name TEXT,
  p_output TEXT,
  p_vendor TEXT,
  p_call_cost NUMERIC
)
RETURNS VOID
LANGUAGE sql
SECURITY DEFINER
SET search_path = public
AS $$
  UPDATE public.critique_runs
     SET raw_outputs       = raw_outputs || jsonb_build_object(p_critic_name, p_output),
         critics_succeeded = array_append(critics_succeeded, p_critic_name),
         vendors_used      = CASE WHEN p_vendor = ANY(vendors_used)
                                  THEN vendors_used
                                  ELSE array_append(vendors_used, p_vendor) END,
         total_cost_usd    = COALESCE(total_cost_usd, 0) + p_call_cost
   WHERE id = p_run_id;
$$;

GRANT EXECUTE ON FUNCTION public.append_critic_success(UUID, TEXT, TEXT, TEXT, NUMERIC) TO service_role;
REVOKE EXECUTE ON FUNCTION public.append_critic_success(UUID, TEXT, TEXT, TEXT, NUMERIC) FROM PUBLIC, anon, authenticated;

-- 3. Record a critic failure (timeout, API error, schema fail).
CREATE OR REPLACE FUNCTION public.append_critic_failure(
  p_run_id UUID,
  p_critic_name TEXT,
  p_error_string TEXT
)
RETURNS VOID
LANGUAGE sql
SECURITY DEFINER
SET search_path = public
AS $$
  UPDATE public.critique_runs
     SET critics_failed = array_append(critics_failed, p_critic_name),
         skip_reason    = skip_reason || jsonb_build_object(p_critic_name, p_error_string)
   WHERE id = p_run_id;
$$;

GRANT EXECUTE ON FUNCTION public.append_critic_failure(UUID, TEXT, TEXT) TO service_role;
REVOKE EXECUTE ON FUNCTION public.append_critic_failure(UUID, TEXT, TEXT) FROM PUBLIC, anon, authenticated;

-- 4. Record a critic skip (data-class gate or cost cap).
CREATE OR REPLACE FUNCTION public.append_critic_skip(
  p_run_id UUID,
  p_critic_name TEXT,
  p_reason TEXT  -- 'cost_cap' or 'data_class'
)
RETURNS VOID
LANGUAGE sql
SECURITY DEFINER
SET search_path = public
AS $$
  UPDATE public.critique_runs
     SET critics_skipped = array_append(critics_skipped, p_critic_name),
         skip_reason     = skip_reason || jsonb_build_object(p_critic_name, p_reason)
   WHERE id = p_run_id;
$$;

GRANT EXECUTE ON FUNCTION public.append_critic_skip(UUID, TEXT, TEXT) TO service_role;
REVOKE EXECUTE ON FUNCTION public.append_critic_skip(UUID, TEXT, TEXT) FROM PUBLIC, anon, authenticated;

-- 5. Boot-time reclaim: unconditional.
CREATE OR REPLACE FUNCTION public.reclaim_orphan_runs()
RETURNS INT
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  n INT;
BEGIN
  UPDATE public.critique_runs
     SET status        = 'failed',
         error_message = 'reclaimed: worker restart before completion'
   WHERE status = 'running';
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END;
$$;

GRANT EXECUTE ON FUNCTION public.reclaim_orphan_runs() TO service_role;
REVOKE EXECUTE ON FUNCTION public.reclaim_orphan_runs() FROM PUBLIC, anon, authenticated;

-- 6. Periodic stuck-job monitor: only runs older than the threshold.
CREATE OR REPLACE FUNCTION public.fail_stuck_runs(p_threshold_minutes INT DEFAULT 10)
RETURNS INT
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  n INT;
BEGIN
  UPDATE public.critique_runs
     SET status        = 'failed',
         error_message = 'stuck: exceeded max processing time'
   WHERE status = 'running'
     AND started_at < now() - make_interval(mins => p_threshold_minutes);
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END;
$$;

GRANT EXECUTE ON FUNCTION public.fail_stuck_runs(INT) TO service_role;
REVOKE EXECUTE ON FUNCTION public.fail_stuck_runs(INT) FROM PUBLIC, anon, authenticated;

-- 7. Release claim on shutdown: requeue in-flight runs.
CREATE OR REPLACE FUNCTION public.release_critique_claims(p_run_ids UUID[])
RETURNS INT
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  n INT;
BEGIN
  UPDATE public.critique_runs
     SET status     = 'queued',
         started_at = NULL
   WHERE id = ANY(p_run_ids)
     AND status = 'running';
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END;
$$;

GRANT EXECUTE ON FUNCTION public.release_critique_claims(UUID[]) TO service_role;
REVOKE EXECUTE ON FUNCTION public.release_critique_claims(UUID[]) FROM PUBLIC, anon, authenticated;

-- 8. Count queued runs (for a queue-depth backpressure check).
CREATE OR REPLACE FUNCTION public.count_queued_runs()
RETURNS INT
LANGUAGE sql
SECURITY DEFINER
SET search_path = public
AS $$
  SELECT COUNT(*)::INT FROM public.critique_runs WHERE status = 'queued';
$$;

GRANT EXECUTE ON FUNCTION public.count_queued_runs() TO service_role;
REVOKE EXECUTE ON FUNCTION public.count_queued_runs() FROM PUBLIC, anon, authenticated;
