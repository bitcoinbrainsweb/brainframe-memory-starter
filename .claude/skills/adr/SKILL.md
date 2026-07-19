---
name: adr
description: >-
  Records architecture decisions. Use when user says: we decided, log a decision,
  record this decision, ADR, note that we chose. Writes to USER/routing/decisions.md.
  Append-only. Never edits existing entries.
version: 1.0.0
---

# ADR -- Architecture Decision Record

Append-only. Never edits or removes existing entries.

---

## Step 1 -- Extract fields

From context, identify:
- `{DECISION}` -- one sentence: what was decided
- `{RATIONALE}` -- one sentence: why this over alternatives
- `{STATUS}` -- CONFIRMED, PROVISIONAL, or SUPERSEDED

If status is unclear, default to CONFIRMED. If superseding a prior decision, ask which one and mark that entry SUPERSEDED in the same write.

---

## Step 2 -- Write to decisions.md

Append to `USER/routing/decisions.md` (or `USER/routing/{project}/decisions.md` if a project is active).

Entry format:
```
---
date: {YYYY-MM-DD}
decision: {DECISION}
rationale: {RATIONALE}
status: {STATUS}
---
```

### claude-code surface

```bash
source ~/.config/memory-starter/.env
BRANCH="${GITHUB_BRANCH:-main}"
FILE_PATH="USER/routing/decisions.md"  # adjust for active project

RESPONSE=$(curl -s -H "Authorization: Bearer ${GITHUB_TOKEN}" \
  "https://api.github.com/repos/${GITHUB_REPO}/contents/${FILE_PATH}?ref=${BRANCH}")
CURRENT=$(echo "$RESPONSE" | python3 -c "import sys,json,base64; d=json.load(sys.stdin); print(base64.b64decode(d['content']).decode())")
SHA=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['sha'])")

NEW_ENTRY="
---
date: {YYYY-MM-DD}
decision: {DECISION}
rationale: {RATIONALE}
status: {STATUS}
---"

UPDATED="${CURRENT}${NEW_ENTRY}"
ENCODED=$(echo "$UPDATED" | python3 -c "import sys,base64; print(base64.b64encode(sys.stdin.buffer.read()).decode())")

curl -s -X PUT "https://api.github.com/repos/${GITHUB_REPO}/contents/${FILE_PATH}" \
  -H "Authorization: Bearer ${GITHUB_TOKEN}" \
  -H "Content-Type: application/json" \
  -d "{\"message\": \"adr: {DECISION[:50]}\", \"content\": \"${ENCODED}\", \"sha\": \"${SHA}\", \"branch\": \"${BRANCH}\"}"
```

### claude-project surface

Produce the entry block for the user to paste into decisions.md. Do not attempt HTTP calls.

---

## Step 3 -- Confirm

Print:
```
Decision recorded: {DECISION}
Status: {STATUS}
Written to: {FILE_PATH}
```

---

## Rules

1. Never edit existing entries -- append only.
2. Status defaults to CONFIRMED if not stated.
3. SUPERSEDED entries are flagged by appending `superseded_by: {new_decision_slug}` -- never deleted.
4. One entry per decision -- do not batch multiple decisions into one entry.
