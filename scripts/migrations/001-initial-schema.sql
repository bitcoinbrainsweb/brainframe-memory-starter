-- Memory Starter v1 Schema
-- Idempotent: safe to run multiple times

-- Enable pgvector extension (optional; used in v2 for semantic search)
-- CREATE EXTENSION IF NOT EXISTS vector;

-- Topics: registry of active projects, clients, matters, domains
CREATE TABLE IF NOT EXISTS topics (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    slug text UNIQUE NOT NULL,
    title text NOT NULL,
    user_id text NOT NULL DEFAULT 'owner',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- Decisions: append-only decision log across all topics
CREATE TABLE IF NOT EXISTS decisions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    topic_slug text,
    decision text NOT NULL,
    rationale text,
    status text NOT NULL DEFAULT 'CONFIRMED', -- CONFIRMED | PROVISIONAL | SUPERSEDED
    user_id text NOT NULL DEFAULT 'owner',
    created_at timestamptz NOT NULL DEFAULT now()
);

-- Specs: plans, analyses, structured documents
CREATE TABLE IF NOT EXISTS specs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    slug text UNIQUE NOT NULL,
    title text NOT NULL,
    topic_slug text,
    status text NOT NULL DEFAULT 'draft', -- draft | active | superseded | archived
    content_path text, -- path to markdown file in GitHub
    user_id text NOT NULL DEFAULT 'owner',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- Roadmap items: follow-ups, open work, deadlines
CREATE TABLE IF NOT EXISTS roadmap_items (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    title text NOT NULL,
    topic_slug text,
    status text NOT NULL DEFAULT 'planned', -- planned | in_progress | shipped | cancelled
    priority int DEFAULT 2, -- 1=high 2=normal 3=low
    due_date date,
    user_id text NOT NULL DEFAULT 'owner',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- People: collaborators, clients, contacts
CREATE TABLE IF NOT EXISTS people (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL,
    role text,
    relationship text, -- collaborator | client | vendor | contact
    notes text,
    user_id text NOT NULL DEFAULT 'owner',
    created_at timestamptz NOT NULL DEFAULT now()
);

-- Contributions: inbox log (pending/promoted/rejected)
CREATE TABLE IF NOT EXISTS contributions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    contributor text NOT NULL,
    subject text NOT NULL,
    topic_slug text,
    status text NOT NULL DEFAULT 'pending', -- pending | promoted | rejected
    file_path text, -- path to contribution file in GitHub
    rejection_note text,
    user_id text NOT NULL DEFAULT 'owner',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- Audit log: every canonical write with author and timestamp
CREATE TABLE IF NOT EXISTS audit_log (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    author text NOT NULL,
    action text NOT NULL, -- write | promote | reject | delete
    target_path text NOT NULL,
    summary text,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- Enable Row Level Security on all tables
ALTER TABLE topics ENABLE ROW LEVEL SECURITY;
ALTER TABLE decisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE specs ENABLE ROW LEVEL SECURITY;
ALTER TABLE roadmap_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE people ENABLE ROW LEVEL SECURITY;
ALTER TABLE contributions ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY;

-- RLS policies: anon role can SELECT all rows; INSERT/UPDATE/DELETE only own rows
-- The anon key is the only client credential. Service role key is never distributed.

CREATE POLICY IF NOT EXISTS "anon_select_topics" ON topics FOR SELECT TO anon USING (true);
CREATE POLICY IF NOT EXISTS "anon_insert_topics" ON topics FOR INSERT TO anon WITH CHECK (user_id = 'owner');
CREATE POLICY IF NOT EXISTS "anon_update_topics" ON topics FOR UPDATE TO anon USING (user_id = 'owner');

CREATE POLICY IF NOT EXISTS "anon_select_decisions" ON decisions FOR SELECT TO anon USING (true);
CREATE POLICY IF NOT EXISTS "anon_insert_decisions" ON decisions FOR INSERT TO anon WITH CHECK (user_id = 'owner');

CREATE POLICY IF NOT EXISTS "anon_select_specs" ON specs FOR SELECT TO anon USING (true);
CREATE POLICY IF NOT EXISTS "anon_insert_specs" ON specs FOR INSERT TO anon WITH CHECK (user_id = 'owner');
CREATE POLICY IF NOT EXISTS "anon_update_specs" ON specs FOR UPDATE TO anon USING (user_id = 'owner');

CREATE POLICY IF NOT EXISTS "anon_select_roadmap" ON roadmap_items FOR SELECT TO anon USING (true);
CREATE POLICY IF NOT EXISTS "anon_insert_roadmap" ON roadmap_items FOR INSERT TO anon WITH CHECK (user_id = 'owner');
CREATE POLICY IF NOT EXISTS "anon_update_roadmap" ON roadmap_items FOR UPDATE TO anon USING (user_id = 'owner');

CREATE POLICY IF NOT EXISTS "anon_select_people" ON people FOR SELECT TO anon USING (true);
CREATE POLICY IF NOT EXISTS "anon_insert_people" ON people FOR INSERT TO anon WITH CHECK (user_id = 'owner');

CREATE POLICY IF NOT EXISTS "anon_select_contributions" ON contributions FOR SELECT TO anon USING (true);
CREATE POLICY IF NOT EXISTS "anon_insert_contributions" ON contributions FOR INSERT TO anon WITH CHECK (true);
CREATE POLICY IF NOT EXISTS "anon_update_contributions" ON contributions FOR UPDATE TO anon USING (true);

CREATE POLICY IF NOT EXISTS "anon_select_audit" ON audit_log FOR SELECT TO anon USING (true);
CREATE POLICY IF NOT EXISTS "anon_insert_audit" ON audit_log FOR INSERT TO anon WITH CHECK (true);
