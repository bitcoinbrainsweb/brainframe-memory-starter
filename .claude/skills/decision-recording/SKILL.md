---
name: decision-recording
description: "Use when the user says \"log a decision\", \"record that we decided\", or \"note this decision\"."
when_to_use: "Use when the user says \"log a decision\", \"record that we decided\", or \"note this decision\"."
disable-model-invocation: true
version: 1.1.0
---

# decision-recording

## Trust

Reads: USER/routing/decisions.md. Writes: Supabase decisions table (insert). External calls: Supabase REST API (POST).

## Instructions

Load credentials:
```bash
source ~/.config/memory-starter/.env
# Expects: SUPABASE_URL, SUPABASE_ANON_KEY
```

Extract from context:
- `{SUMMARY}` — one sentence: what was decided
- `{OUTCOME}` — why, or what it replaces
- `{TAGS}` — comma-separated keywords (optional, default empty array)

Insert to Supabase:
```bash
curl -s -X POST "${SUPABASE_URL}/rest/v1/decisions" \
  -H "apikey: ${SUPABASE_ANON_KEY}" \
  -H "Authorization: Bearer ${SUPABASE_ANON_KEY}" \
  -H "Content-Type: application/json" \
  -H "Prefer: return=representation" \
  -d '{
    "summary": "{SUMMARY}",
    "outcome": "{OUTCOME}",
    "tags": ["{TAGS}"],
    "user_id": "owner"
  }'
```

On success (HTTP 201), confirm to user: "Decision recorded: {SUMMARY}"
On failure, show the full response body.

For claude-project surface: produce the curl command above for the user to run. Do not attempt to execute it.
