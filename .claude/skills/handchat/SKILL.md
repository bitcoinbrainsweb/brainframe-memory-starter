---
name: handchat
description: >-
  Mid-session pause for immediate continuation in a new chat. Use when user says:
  handchat, pause here, continue in new chat, context is getting long. Writes
  context brief to sessions.md with a pickup slug. New chat resumes via pickup {slug}.
version: 1.0.0
---

# Handchat — Mid-Session Pause

Time budget: under 60 seconds. One file write. No session close.

---

## What this is NOT

- Not a session close — run quitchat for that
- Not a memory audit
- Not a deliverable scan

---

## Step 1 — Derive pickup slug

2-4 word kebab-case slug describing what is being worked on.

Rules:
- Specific enough to recall unambiguously ("phase2-spec" not "spec")
- Lowercase, kebab-case
- Lead with a project-specific term if possible

Examples: `api-rate-limit-fix`, `onboarding-flow-redesign`, `q3-pricing-decision`

---

## Step 2 — Write context brief

Two fields:

**next_action** (what to do first in new chat, under 300 chars):
```
Last: {one sentence — what just finished}
Pick up: {one sentence — exactly what to do first}
Do not redo: {comma-separated — things already done}
```

**context_brief** (intelligence for new chat):
```
## Mental model
{Current understanding — what we know, what frame we are in}

## Decisions made
- {Decision}: {why}

## Approaches that failed
- {what}: {why}

## Open questions
- {question}
```

Be generous. The new chat reads this and becomes as capable as this chat.

---

## Step 3 — Write to sessions.md

Detect active project. Write to `USER/routing/{project}/sessions.md` or `USER/routing/sessions.md`.

Append a handchat entry:
```
### {YYYY-MM-DD} — HANDCHAT: {slug}
**next_action:** {next_action}
**context_brief:**
{context_brief}
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

# Append handchat entry to current content
UPDATED="${CURRENT}

### {YYYY-MM-DD} — HANDCHAT: {slug}
**next_action:** {next_action}
**context_brief:**
{context_brief}
"

ENCODED=$(echo "$UPDATED" | python3 -c "import sys,base64; print(base64.b64encode(sys.stdin.buffer.read()).decode())")

curl -s -X PUT "https://api.github.com/repos/${GITHUB_REPO}/contents/${FILE_PATH}" \
  -H "Authorization: Bearer ${GITHUB_TOKEN}" \
  -H "Content-Type: application/json" \
  -d "{\"message\": \"handchat: {slug}\", \"content\": \"${ENCODED}\", \"sha\": \"${SHA}\", \"branch\": \"${BRANCH}\"}"
```

### claude-project surface

Produce the handchat entry block for the user to paste into the sessions file. Do not attempt HTTP calls.

---

## Step 4 — Surface

Print:
```
handchat saved — pickup slug: {slug}

Rename this chat: {slug} handoff
Start new chat and type: pickup {slug}
```

---

## Failure modes

| Symptom | Action |
|---------|--------|
| GitHub write fails | Print next_action and context_brief inline for manual copy; do not block |
| No active project | Write to root USER/routing/sessions.md |
