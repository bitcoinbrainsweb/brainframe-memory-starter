# USER Folder

**This folder is yours.** Upstream updates never touch anything here — it's protected by `.templatesyncignore` at the repo root.

## What goes here

- **`routing/PROJECTS.md`** — your registry of projects. Add a line for each project you set up.
- **`routing/{project}/`** — one folder per project, each with four files:
  - `facts.md` — IMMUTABLE (edit only via formal ADR)
  - `preferences.md` — MUTABLE (edit at session close)
  - `decisions.md` — APPEND-ONLY (new entries at session close, never edit old ones)
  - `sessions.md` — ROLLING (auto-managed by `quitchat`)

## How to add a new project

1. Click **Add file → Create new file** in GitHub
2. Type `routing/my-new-project/facts.md` — GitHub creates the folder
3. Copy contents from `SYSTEM/templates/facts.md` and paste
4. Repeat for `preferences.md`, `decisions.md`, `sessions.md`
5. Edit `routing/PROJECTS.md` to register the new project

## How to delete the example

Delete the `routing/example-project/` folder. Remove its entry from `routing/PROJECTS.md`. Done.

## What NOT to put here

Don't put secrets, API keys, credentials, or anything sensitive — even if your fork is private. Use a secrets manager instead.

Don't put large files (>1MB). Routing files should stay under their size caps (see the core spec).
