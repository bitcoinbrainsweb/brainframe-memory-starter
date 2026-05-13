---
name: quitchat
description: "Use when the user says "quitchat", "wrap up", "end session", or "we are done"."
when_to_use: "Use when the user says "quitchat", "wrap up", "end session", or "we are done"."
disable-model-invocation: false
version: 1.0.0
---

# quitchat

## Trust

Reads: full session context. Writes: USER/routing/sessions.md (append), Supabase audit_log (insert). External calls: GitHub API (PUT), Supabase REST API (POST).

## Instructions

[Skill logic goes here. This is a stub. Implement per your surface (claude-code or claude-project).]

For claude-code: execute curl calls to Supabase REST API. Load credentials from ~/.config/memory-starter/.env.

For claude-project: produce the curl command or SQL for the user to run manually.
