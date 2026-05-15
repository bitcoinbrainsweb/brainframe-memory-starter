---
name: quitchat
description: "Use when the user says \"quitchat\", \"wrap up\", \"end session\", or \"we are done\"."
when_to_use: "Use when the user says \"quitchat\", \"wrap up\", \"end session\", or \"we are done\"."
disable-model-invocation: true
version: 1.1.0
---

# quitchat

## Trust

Reads: full session context. Writes: Supabase audit_log (insert). External calls: Supabase REST API (POST).

## Instructions

Load credentials:
```bash
source ~/.config/memory-starter/.env
# Expects: SUPABASE_URL, SUPABASE_ANON_KEY
```

### Step 1 — Scan session

Review the full conversation. Extract:
- `{SUMMARY}` — 2-3 sentences: what was decided or built
- `{DECISIONS}` — list of decisions made (slug + one line each), or "none"
- `{OPEN_ITEMS}` — anything unresolved or deferred

Present to user for confirmation before writing.

### Step 2 — Write audit log

```bash
curl -s -X POST "${SUPABASE_URL}/rest/v1/audit_log" \
  -H "apikey: ${SUPABASE_ANON_KEY}" \
  -H "Authorization: Bearer ${SUPABASE_ANON_KEY}" \
  -H "Content-Type: application/json" \
  -H "Prefer: return=representation" \
  -d '{
    "summary": "{SUMMARY}",
    "decisions": "{DECISIONS}",
    "open_items": "{OPEN_ITEMS}",
    "user_id": "owner"
  }'
```

On success (HTTP 201), confirm: "Session closed. Audit log written."
On failure, show the full response body and suggest the user copy the summary manually.

### Step 3 — Close

Print:
```
Session closed.
Summary: {SUMMARY}
Open items: {OPEN_ITEMS}
```

For claude-project surface: produce the curl command above for the user to run. Do not attempt to execute it.
