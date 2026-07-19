# LOOKUP -- Routers

Tells Claude where to look for specific content. Keep this file tight.

(Renamed from TIER_B.md. Routing tables live here.)

## On session start

Always fetch:
- `SYSTEM/GLOBAL_RULES.md` -- universal rules
- `USER/routing/PROJECTS.md` -- your list of registered projects
- `USER/people.md` -- collaborators, clients, contacts

## On detecting an active project

When the user mentions a project by name (matching an entry in `PROJECTS.md`), fetch from `USER/routing/{project}/`:
- `facts.md` -- IMMUTABLE per-project facts
- `preferences.md` -- MUTABLE per-project preferences
- `decisions.md` -- APPEND-ONLY decision log
- `sessions.md` -- ROLLING session summaries

## On project switch mid-chat

The user may signal with:
- Explicit: "switching to X", "working on X now"
- Scoped: `@projectname` at the start of a message (applies to that exchange only)
- Keyword: the project name appears in the user's message

On switch: fetch the new project's four routing files. Flag the previous project as secondary; don't forget it.

## Trigger grammar

- `000` -> re-fetch GLOBAL_RULES + LOOKUP
- `001` -> re-fetch the active project's four routing files
- `002` -> full refresh (GLOBAL_RULES + LOOKUP + all active routing), bypass cache
- `@project` -> scope the next exchange to `project`, then reset
- `recall: {topic}` -> search the queryable store (or topic notes) for `{topic}`

## On missing files

- `PROJECTS.md` missing: the user is on a fresh fork. Offer to create it and populate the example project.
- `{project}/facts.md` missing: the user named a project that isn't registered. Offer to create the folder with templates from `SYSTEM/templates/`.
- `SYSTEM/GLOBAL_RULES.md` missing: something broke badly. Tell the user; operate on T1 only.

## Writer rules

Only the `quitchat` skill writes to `preferences.md`, `decisions.md`, and `sessions.md`. Only the ADR skill writes to `facts.md`. In-chat edits are not direct writes; they become queued confirmations for the next quitchat.

## Bootstrap

On a brand-new fork with no routing yet: operate on GLOBAL_RULES alone, offer to set up the example project. Never fail silently.
