# Brainframe Memory Starter

**A layered memory system for Claude.**

Forget less. Re-explain less. Works across multiple projects without bloating your context window.

---

## What this is

A fork-and-paste setup that gives Claude a consistent memory architecture across your projects. Based on a six-layer model: universal rules, routers, per-project state, canonical specs, and (optionally) semantic graph memory.

Three tiers:

| Tier | What you get | What you need |
|------|--------------|---------------|
| **0** — Files only | Per-project memory, refresh grammar, project switching | Just a GitHub account. 10-min setup. |
| **1** — Observable | + event log, weekly health report, drift detection | Add a free Supabase project |
| **2** — Full stack | + semantic graph memory via Graphiti | Self-host Graphiti (~$5/mo VPS) |

**Start at Tier 0.** Upgrade later if you need it.

---

## Quick start (Tier 0, ~10 minutes, no terminal)

1. **Fork this repo.** Click "Use this template" → "Create a new repository" → name it something like `memory-{yourname}`. Make it **public** (see note below).

2. **Open `SYSTEM/PROJECT_INSTRUCTIONS.md`** in your new fork. Click the "Raw" button and copy the whole file.

3. **Create a Claude Project** (claude.ai → Projects → New). Paste into Project Instructions. Replace `YOUR_USERNAME/YOUR_FORK` placeholder with your actual repo slug.

4. **Enable GitHub Actions** in your fork (Settings → Actions → General → Allow all actions). This lets you receive update PRs.

5. **Done.** Start a new chat in that Project. Claude will fetch routing on first message.

### Public vs private fork

Claude can read **public** forks directly via `web_fetch`. If you make your fork **private**, you'll need to upload your routing files as Claude Project Knowledge, and re-upload them when they change. Public is the better experience.

---

## Adding a new project

1. In your fork on GitHub, navigate to `USER/routing/`.
2. Click **Add file → Create new file**.
3. Type `my-project/facts.md` — GitHub creates the folder.
4. Copy contents from `SYSTEM/templates/facts.md` into it.
5. Repeat for `preferences.md`, `decisions.md`, `sessions.md`.
6. Edit `USER/routing/PROJECTS.md` to register the new project name.

Next chat: mention your project by name. Claude detects and fetches the routing.

---

## The refresh grammar

Type these into chat to force Claude to refresh its memory:

| Trigger | What it does |
|---------|--------------|
| `000` | Reload universal rules + routers |
| `001` | Reload current project's state |
| `002` | Full reload, bypass cache |
| `@projectname` | Use a specific project for this one message |
| `recall: topic` | Force a Graphiti query (Tier 2 only) |

---

## What lives where

```
your-fork/
├── SYSTEM/                    ← updated by upstream sync
│   ├── TIER_A.md              Universal rules (always loaded)
│   ├── TIER_B.md              Routers (tells Claude where to look)
│   ├── PROJECT_INSTRUCTIONS.md  Paste this into your Claude Project
│   └── templates/             Starting templates for new projects
└── USER/                      ← yours, never overwritten
    ├── README.md
    └── routing/
        ├── PROJECTS.md        Your project index
        └── example-project/   (yours to modify, delete, or model off of)
            ├── facts.md       IMMUTABLE — edit via ADR only
            ├── preferences.md MUTABLE — edit at session close
            ├── decisions.md   APPEND-ONLY — new entries at session close
            └── sessions.md    ROLLING — auto-managed
```

The `USER/` folder is **protected from upstream updates**. Anything you put there is yours permanently.

---

## Receiving updates

When the upstream template is updated, you'll get a PR in your fork titled `[Template Sync] v1.X.0`. Review the diff, merge if you want the update. Your `USER/` folder is untouched.

---

## Upgrade to Tier 1 or Tier 2

See `SYSTEM/upgrades/` once you're comfortable with Tier 0. Tier 1 adds a Supabase event log; Tier 2 adds a self-hosted Graphiti instance for semantic memory. Each tier is sequential — you can't skip from 0 to 2.

---

## Philosophy

Claude's attention dips in the middle of long conversations (U-shaped curve). This system gives you explicit control over re-injection via `000`/`001`/`002`, a clear separation between immutable facts and mutable preferences, and graceful degradation if any layer fails. You never lose the thread.

---

## License

MIT. Fork freely, modify, share.

---

**Version:** See `VERSION` file.
**Spec:** `memory-system-v2.md` and `memory-system-distribution.md` in the upstream canonical repo.
**Issues:** Open an issue in the upstream `brainframe-memory-master` repo.
