---
name: recall
description: "Use when the user asks to find, search, recall, or look up anything from memory."
when_to_use: "Use when the user asks to find, search, recall, or look up anything from memory."
disable-model-invocation: false
version: 1.0.0
---

# recall

## Trust

Reads: GitHub canonical files, Supabase via REST API. Writes: nothing. External calls: Supabase REST API (read-only).

## Instructions

[Skill logic goes here. This is a stub. Implement per your surface (claude-code or claude-project).]

For claude-code: execute curl calls to Supabase REST API. Load credentials from ~/.config/memory-starter/.env.

For claude-project: produce the curl command or SQL for the user to run manually.
