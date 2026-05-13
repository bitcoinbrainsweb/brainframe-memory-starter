---
name: contribute
description: "Use when the user says "contribute", "add a note", or "I want to log something" in a Contributor session."
when_to_use: "Use when the user says "contribute", "add a note", or "I want to log something" in a Contributor session."
disable-model-invocation: false
version: 1.0.0
---

# contribute

## Trust

Reads: nothing. Writes: contributions/{date}-{name}-{slug}.md only. External calls: GitHub API (PUT).

## Instructions

[Skill logic goes here. This is a stub. Implement per your surface (claude-code or claude-project).]

For claude-code: execute curl calls to Supabase REST API. Load credentials from ~/.config/memory-starter/.env.

For claude-project: produce the curl command or SQL for the user to run manually.
