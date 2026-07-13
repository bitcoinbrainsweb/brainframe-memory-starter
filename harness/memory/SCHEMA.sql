-- Memory system: queryable audit trail (state_events) for a database-backed memory.
-- Apply this to your own Postgres / Supabase project. Runnable standalone.
--
-- This is the append-only event log that records every change to a tracked
-- entity: who did it (actor), what kind of change (event_type), and the full
-- before/after snapshot as JSONB. It is the substance behind "Claude that
-- remembers": every decision, note, and status change lands here and stays
-- queryable. The optional session_id column links an event to the session it
-- happened in (see the sessions system); it is a plain uuid here so this schema
-- installs on its own, and you can add the foreign key later if you also install
-- the sessions table.

BEGIN;

-- Trigram index support for fuzzy search over event content.
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- Shared helper: stamp updated_at on any row update. Safe to run more than once.
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TABLE IF NOT EXISTS state_events (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  entity_type  text NOT NULL,              -- e.g. 'decision', 'note', 'session', 'card'
  entity_id    uuid NOT NULL,
  entity_slug  text,                        -- optional human-readable handle
  event_type   text NOT NULL,              -- e.g. 'created', 'updated', 'closed'
  actor        text NOT NULL,              -- who caused the change (agent or human role)
  before       jsonb,                       -- entity snapshot before the change (null on create)
  after        jsonb,                       -- entity snapshot after the change (null on delete)
  session_id   uuid,                        -- optional: references sessions(id) if you install the sessions system
  created_at   timestamptz NOT NULL DEFAULT now()
);

-- RLS on: the service role bypasses; every other role is blocked until you add policies.
ALTER TABLE state_events ENABLE ROW LEVEL SECURITY;
CREATE POLICY pol_state_events_service_all ON state_events FOR ALL TO service_role USING (true) WITH CHECK (true);

CREATE INDEX IF NOT EXISTS idx_state_events_entity ON state_events (entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_state_events_created_desc ON state_events (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_state_events_session ON state_events (session_id) WHERE session_id IS NOT NULL;

COMMIT;
