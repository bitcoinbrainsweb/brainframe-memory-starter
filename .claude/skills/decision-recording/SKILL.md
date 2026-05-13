---
name: decision-recording
description: "Use when the user says "log a decision", "record that we decided", or "note this decision"."
when_to_use: "Use when the user says "log a decision", "record that we decided", or "note this decision"."
disable-model-invocation: false
version: 1.0.0
---

# decision-recording

## Trust

Reads: USER/routing/decisions.md. Writes: USER/routing/decisions.md (append), Supabase decisions table (insert). External calls: GitHub API (PUT), Supabase REST API (POST).

## Instructions

[Skill logic goes here. This is a stub. Implement per your surface (claude-code or claude-project).]

For claude-code: execute curl calls to Supabase REST API. Load credentials from ~/.config/memory-starter/.env.

For claude-project: produce the curl command or SQL for the user to run manually.
