-- Sessions system: session tracking with a handoff chain.
-- Apply this to your own Postgres / Supabase project. Runnable standalone.
--
-- A "session" is one working conversation. Rows go IDLE rather than closing:
-- a session that stops being touched keeps its last_seen_at and status, so
-- current state is inferred from those, not from an explicit close. The
-- handoff_chain_id self-reference links a session to the one it continued from,
-- so a long-running thread of work is a chain of session rows.

BEGIN;

-- Shared helper: stamp updated_at on any row update. Safe to run more than once.
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TABLE IF NOT EXISTS sessions (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project           text NOT NULL,
  chat_url          text,
  chat_title        text,
  started_at        timestamptz NOT NULL DEFAULT now(),
  ended_at          timestamptz,
  last_seen_at      timestamptz NOT NULL DEFAULT now(),
  status            text NOT NULL,
  -- self-reference: the session this one continued from (the head of the handoff chain
  -- points at itself). DEFERRABLE so a fresh chain head can insert with its own id.
  handoff_chain_id  uuid NOT NULL REFERENCES sessions(id) DEFERRABLE INITIALLY DEFERRED,
  summary           text,
  context_brief     text,             -- generous handoff brief written by handchat, read by pickup
  velocity_score    numeric,
  exchanges         int,
  tier              text,
  pickup_slug       text,             -- short unique-per-project handle set by handchat, consumed by pickup
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now(),
  -- replace with your own project slugs
  CONSTRAINT chk_sessions_project CHECK (project IN ('project_a','project_b')),
  CONSTRAINT chk_sessions_status CHECK (status IN ('active','closed','handed_off')),
  CONSTRAINT chk_sessions_tier CHECK (tier IN ('context_save','full_close') OR tier IS NULL)
);

CREATE TRIGGER trg_sessions_updated_at
  BEFORE UPDATE ON sessions FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- RLS on: the service role bypasses; every other role is blocked until you add policies.
ALTER TABLE sessions ENABLE ROW LEVEL SECURITY;
CREATE POLICY pol_sessions_service_all ON sessions FOR ALL TO service_role USING (true) WITH CHECK (true);

-- At most one active session per (project, chat_url).
CREATE UNIQUE INDEX IF NOT EXISTS uq_sessions_active_chat ON sessions (project, chat_url) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_sessions_project_status ON sessions (project, status);
CREATE INDEX IF NOT EXISTS idx_sessions_status_active ON sessions (status) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_sessions_handoff_chain ON sessions (handoff_chain_id) WHERE status != 'closed';
-- last_seen_at on active rows is how you find idle sessions (status stays 'active' until closed).
CREATE INDEX IF NOT EXISTS idx_sessions_last_seen_active ON sessions (last_seen_at) WHERE status = 'active';
-- pickup_slug is unique per project among sessions that have one (enforces handchat collision guard).
CREATE UNIQUE INDEX IF NOT EXISTS uq_sessions_pickup_slug ON sessions (project, pickup_slug) WHERE pickup_slug IS NOT NULL;

COMMIT;

-- ---------------------------------------------------------------------------
-- OPTIONAL: velocity tracking.
-- A separate, denormalized table for measuring output per working session:
-- exchanges, commits, lines changed, and a weighted score. Independent of the
-- sessions table above; install it only if you want velocity metrics.
-- ---------------------------------------------------------------------------

BEGIN;

CREATE TABLE IF NOT EXISTS velocity_sessions (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project          text NOT NULL,
  -- replace with your own category slugs
  project_category text NOT NULL CHECK (project_category IN ('category_a','category_b')),
  session_date     date NOT NULL,
  session_start    timestamptz,
  session_end      timestamptz,
  duration_min     integer,
  exchange_count   integer NOT NULL DEFAULT 0,
  specs_written    integer NOT NULL DEFAULT 0,
  agent_prompts    integer NOT NULL DEFAULT 0,
  commits          integer NOT NULL DEFAULT 0,
  loc_delta        integer NOT NULL DEFAULT 0,
  weighted_score   numeric(8,2) NOT NULL DEFAULT 0,
  is_noise         boolean NOT NULL DEFAULT false,
  source           text NOT NULL DEFAULT 'live' CHECK (source IN ('live','backfill')),
  notes            text,
  -- multi-project sessions: arrays supplement the primary project/category above
  projects         text[] NOT NULL DEFAULT '{}',
  categories       text[] NOT NULL DEFAULT '{}',
  project_phase    text CHECK (project_phase IN ('discovery','architecture','build','maintenance')) DEFAULT NULL,
  session_type     text NOT NULL DEFAULT 'delivery' CHECK (session_type IN ('delivery','skill-building','planning','debugging')),
  -- generated: score normalized per exchange
  output_per_exchange numeric(8,4) GENERATED ALWAYS AS (
    CASE WHEN exchange_count > 0 THEN ROUND((weighted_score / exchange_count)::numeric, 4) ELSE 0 END
  ) STORED,
  created_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_velocity_sessions_project ON velocity_sessions (project);
CREATE INDEX IF NOT EXISTS idx_velocity_sessions_date ON velocity_sessions (session_date DESC);
CREATE INDEX IF NOT EXISTS idx_velocity_sessions_category ON velocity_sessions (project_category);
CREATE UNIQUE INDEX IF NOT EXISTS uq_velocity_sessions_backfill
  ON velocity_sessions (session_date, project, source)
  WHERE source = 'backfill';

COMMIT;
