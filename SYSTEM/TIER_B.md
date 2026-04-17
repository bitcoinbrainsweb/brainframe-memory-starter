# Tier B — Routers

Tells Claude where to look for specific content. Keep this file under 2000 chars.

## On session start

Always fetch:
- `USER/routing/PROJECTS.md` — your list of registered projects

## On detecting an active project

When the user mentions a project by name (matching an entry in `PROJECTS.md`), fetch from `USER/routing/{project}/`:
- `facts.md` — IMMUTABLE per-project facts
- `preferences.md` — MUTABLE per-project preferences
- `decisions.md` — APPEND-ONLY decision log
- `sessions.md` — ROLLING session summaries

## On project switch mid-chat

User may signal with:
- Explicit: "switching to X", "working on X now"
- Scoped: `@projectname` at start of message (applies to this exchange only)
- Keyword: project name appears in user's message

On switch: fetch the new project's four routing files. Flag previous project as secondary — don't forget it.

## On trigger phrases

- `000` → re-fetch TIER_A + TIER_B
- `001` → re-fetch active project's four routing files
- `002` → full refresh (TIER_A + TIER_B + all active routing), bypass cache
- `recall: {topic}` → (Tier 2 only) query Graphiti regardless of domain gate

## On missing files

- If `PROJECTS.md` is missing: user is on a fresh fork. Offer to create it and populate the example project.
- If `{project}/facts.md` is missing: user mentioned a project not registered. Offer to create the folder with templates from `SYSTEM/templates/`.
- If `SYSTEM/TIER_A.md` is missing: something broke badly. Tell the user.

## Writer rules

Only the `quitchat` skill writes to `preferences.md`, `decisions.md`, `sessions.md`. Only the ADR skill writes to `facts.md`. In-chat edits are NOT direct writes — they become queued confirmations for the next quitchat.

## Bootstrap

If the user is on a brand-new fork with no routing yet: operate on TIER_A alone, offer to set up the example project. Don't fail silently.
