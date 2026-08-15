---
name: quitchat
description: >-
  Session-close orchestrator. Use when user says: quitchat, wrap up, end session,
  close this out, done for today. Scans open state, writes the session summary to
  sessions.md, runs the memory audit, records a velocity row, then closes.
version: 1.0.0
---

# Quitchat -- Session Close

Full close. Writes durable state. Runs to completion even when a step fails.

---

## What this is NOT

- Not a mid-session pause -- run handchat for that
- Not a decision log -- run adr for that
- Not a summary printed in chat. Nothing is closed until it is persisted.

---

## Orchestration order (hard requirement)

```
scan -> save -> audit -> VELOCITY WRITE -> close
```

The velocity write runs after the audit so any applied memory changes are already
reflected in the row. Do not reorder. Under context pressure run `scan -> save`
only, say so, and stop; do not silently skip the tail.

| Step | Action | Purpose |
|------|--------|---------|
| scan | classify the session, count exchanges, raise open items | know what happened |
| save | write the session summary to `sessions.md` | persist context |
| audit | run the memory-audit write gate over stored memory | keep memory lean |
| VELOCITY WRITE | insert one `velocity_sessions` row | record session metadata |
| close | set the session row to `closed`, print the receipt | end cleanly |

---

## Step 1 -- Scan

Read back the conversation and produce four fields.

- **summary**: 2-3 sentences. What was decided, built, or learned. Not a transcript.
- **open_items**: anything unresolved that the next session must pick up. Empty list is a valid answer; say so rather than inventing filler.
- **queued writes**: any decision reached this session that is not yet in `USER/routing/decisions.md`, and any working preference the user stated that is not yet in `USER/routing/preferences.md`. Surface both as proposals. Do not write either file here; `adr` owns `decisions.md` and `preferences.md` is edited by hand once confirmed. See `SYSTEM/LOOKUP.md` > Writer rules.
- **counters**: exchange count, commits, lines changed. Measure them. If you cannot measure a counter, record it as null rather than guessing.

---

## Step 2 -- Save

Detect the active project. Write to `USER/routing/{project}/sessions.md`, or
`USER/routing/sessions.md` when no project is active.

`sessions.md` is a rolling last-5 file: append the new entry, then remove the
oldest if the file now holds six.

```
---
date: {YYYY-MM-DD}
summary: {2-3 sentences}
open_items: [{item}, {item}]
---
```

### claude-code surface

```bash
source ~/.config/memory-starter/.env
# Expects: GITHUB_TOKEN, GITHUB_REPO, GITHUB_BRANCH (default: main)

BRANCH="${GITHUB_BRANCH:-main}"
FILE_PATH="USER/routing/sessions.md"  # adjust for active project

RESPONSE=$(curl -s -H "Authorization: Bearer ${GITHUB_TOKEN}" \
  "https://api.github.com/repos/${GITHUB_REPO}/contents/${FILE_PATH}?ref=${BRANCH}")
CURRENT=$(echo "$RESPONSE" | python3 -c "import sys,json,base64; d=json.load(sys.stdin); print(base64.b64decode(d['content']).decode())")
SHA=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['sha'])")

# Append the session entry, then trim to the newest 5 before encoding
ENCODED=$(printf '%s\n' "$CURRENT" "$SESSION_ENTRY" | python3 -c "import sys,base64; print(base64.b64encode(sys.stdin.buffer.read()).decode())")

curl -s -X PUT "https://api.github.com/repos/${GITHUB_REPO}/contents/${FILE_PATH}" \
  -H "Authorization: Bearer ${GITHUB_TOKEN}" \
  -H "Content-Type: application/json" \
  -d "{\"message\": \"quitchat: session {YYYY-MM-DD}\", \"content\": \"${ENCODED}\", \"sha\": \"${SHA}\", \"branch\": \"${BRANCH}\"}"
```

### claude-project surface

Produce the session entry block for the user to paste into `sessions.md`. Do not
attempt HTTP calls.

---

## Step 3 -- Audit

Run the memory audit described in `harness/memory/audit/memory-audit.md`. It scores
every stored memory entry against the six write gates and returns one verdict per
entry (KEEP / TIGHTEN / MOVE_TO_FILE / MOVE_TO_STATE / REMOVE).

1. If every entry is KEEP, say "memory clean" and continue to the velocity write.
2. If it raises flags, surface the report table inline and prompt
   `apply N,M / apply all / skip`. Apply only what the user approves.
3. Append one line per applied verdict to the audit log
   (`harness/memory/audit/audit-log.template.md` is the format).
4. The audit never blocks quitchat. If the user skips or does not answer, log
   "audit run, changes deferred" and continue.

If the harness is not installed, skip this step and say so. Do not fabricate a
verdict table.

---

## Step 4 -- Velocity write

Optional, and only if you installed the `velocity_sessions` table from
`harness/sessions/SCHEMA.sql`. One row per session:

```sql
INSERT INTO velocity_sessions (project, project_category, session_date, exchange_count, commits, loc_delta, weighted_score)
VALUES ('{project}', '{category}', current_date, {exchanges}, {commits}, {loc_delta}, {score});
```

---

## Step 5 -- Close

Close the session row from `harness/sessions/SCHEMA.sql`. Status moves to `closed`,
or `handed_off` if a handchat chain is still open.

```sql
UPDATE sessions
   SET status = 'closed',
       ended_at = now(),
       summary = :summary,
       last_seen_at = now()
 WHERE id = '{current_session_id}';
```

Then print:

```
session closed -- {YYYY-MM-DD}
written: {file path}
open items: {n}
memory audit: {clean | n flagged, m applied | skipped}
```

---

## Trust

Writes to `USER/routing/sessions.md` (or the active project's copy) through the
GitHub contents API, and to the `sessions` and `velocity_sessions` tables if the
sessions harness is installed. Reads credentials only from
`~/.config/memory-starter/.env` or session context; never from the project
directory, never inlined. See `.env.example` for the variable names. Applies memory
changes only with explicit user approval, never automatically.

---

## Failure modes

| Symptom | Action |
|---------|--------|
| GitHub write fails | Print the session entry inline for manual copy; continue to audit |
| Memory harness not installed | Skip the audit, say so, continue |
| Audit source unreachable | Continue with a partial audit; name what was skipped |
| Apply step errors mid-run | Stop the apply loop, surface what succeeded, close anyway |
| No database configured | Skip steps 4 and 5; the file write is the durable record |
| Context nearly exhausted | Run `scan -> save` only, tell the user the tail was not run |
