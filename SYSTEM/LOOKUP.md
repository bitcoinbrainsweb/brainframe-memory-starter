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

## On demand (T3)

Never fetched at boot. Fetch when the request needs them, once per session.

- `USER/topics/{slug}.md` -- topic notes and specs. This is what `recall: {topic}` searches when there is no queryable store.
- `USER/voice/_floor.md` plus `USER/voice/authors/{name}.md` -- required before any "write as {name}" request. Fetch fresh; never work from a remembered floor.
- `advisors/INDEX.md`, then `advisors/{category}/{slug}.md` -- required before any "ask {name}" request.
- `harness/{system}/SETUP.md` -- only if that harness system is installed. The harness is optional; a fork with no database uses none of it.

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

Each routing file has a declared set of writers. Anything not listed here is written by hand.

| File | Written by | Mode |
|---|---|---|
| `sessions.md` | `quitchat` (session entries), `handchat` (HANDCHAT entries), `fork-off` (FORK entries) | append; quitchat trims to the newest 5 session entries |
| `decisions.md` | `adr` | append-only, never edits |
| `preferences.md` | nothing automatic; `quitchat` surfaces proposed changes at close | MUTABLE, edited by hand once confirmed |
| `facts.md` | nothing automatic | IMMUTABLE, edited by hand |
| `USER/topics/{slug}.md` | `spec-writing` | one spec per file |

In-chat edits are not direct writes; they become queued confirmations for the next quitchat.

## Bootstrap

On a brand-new fork with no routing yet: operate on GLOBAL_RULES alone, offer to set up the example project. Never fail silently.
