# Memory System v1.2.0

Surface: [OWNER set this to: claude-code OR claude-project]

## Why

This system gives Claude durable memory across sessions using GitHub as canonical store and Supabase as queryable store. Every session starts by fetching context from GitHub. Every session ends by writing a summary back.

## What

Four tiers:
- T1: This file + Project instructions (always present)
- T2: Fetched each session -- GLOBAL_RULES, LOOKUP, routing files, people.md
- T3: Fetched on demand -- topic notes, decisions, specs, voice files, advisor files
- T4: Supabase -- queryable decisions, specs, people, contributions

## How

Session start: fetch T2 files from GitHub. Greet with pending items count if any.
Session end: run `quitchat` to write the session summary and audit log.

Files are canonical. If GitHub and Supabase ever conflict, the GitHub file wins. Treat ambient memory as a lossy pointer; fetch the referenced file before acting.

## Security

Never hardcode credentials. Never read `.env` files from the project directory. Load credentials only from `~/.config/memory-starter/.env` (Claude Code) or from session context (claude.ai Projects). Before executing any skill that writes to an external system, read that skill's Trust section.

## Skills

Skills live in `.claude/skills/`. Each has a `SKILL.md` with frontmatter.

**Session management:** `quitchat`, `handchat`, `pickup`, `fork-off`

**Thinking and planning:** `brainstorming`, `grill-me`, `spec-writing`, `research-council`, `adr`

**Building and writing:** `prompt-writing`, `design`, `human-writing`

**Intelligence:** `ai-pulse`

**Voice cloning:** Built into `human-writing`. Say "write as [name]" or "in [name]'s voice". Add authors to `USER/voice/authors/`. See `USER/voice/README.md`. The voice pipeline is mandatory and un-fakeable: fetch the floor, draft, run the floor check as code, iterate until clean.

**Advisors:** `advisor`. Say "ask [name]" or "run this through [name]". Add advisors to `advisors/[category]/`. See `advisors/README.md`.

## Version

Memory System v1.2.0 -- canonical source: https://github.com/YOUR_GITHUB_USER/YOUR_REPO/blob/main/CLAUDE.md
If your Claude Project instructions differ from this file, this file wins. Update your Project instructions.
