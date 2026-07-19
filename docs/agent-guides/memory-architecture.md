# Memory Architecture

Four tiers. Each fetched differently.

## T1 -- Per-turn ambient (always present)
Your Claude Project instructions + this CLAUDE.md + anything you upload in the current turn. No fetching required. Claude has this at all times.

## T2 -- Per-session fetched (fetched at boot)
Files Claude fetches at the start of every session from your GitHub repo:
- SYSTEM/GLOBAL_RULES.md
- USER/routing/facts.md
- USER/routing/preferences.md
- USER/people.md

These give Claude your universal context without you having to re-explain it.

## T3 -- On-demand fetched (fetched when needed)
Files Claude fetches when you ask about a specific topic, decision, or spec:
- USER/topics/{slug}/decisions.md
- USER/topics/{slug}/notes.md
- contributions/ inbox

Claude fetches these mid-session when relevant, not at boot.

## T4 -- Queryable (Supabase)
Your Supabase project. Tables: decisions, specs, roadmap_items, topics, people, contributions, audit_log.

Claude queries these when you ask it to recall, search, or find something across all your topics.

## How it works together

Boot: T1 always present + T2 fetched. Claude greets you with pending contributions count.
Session: T3 fetched as topics come up. T4 queried for cross-topic recall.
Close: quitchat writes session summary to USER/routing/sessions.md (T2) and audit_log (T4).

## Files vs database

The GitHub repo is the canonical source of truth. Supabase is the queryable index. If they ever conflict, the GitHub file wins.
