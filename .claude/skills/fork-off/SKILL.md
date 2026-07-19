---
name: fork-off
description: >-
  Captures a tangent idea as its own resumable thread without disturbing the current
  session. Triggers ONLY at start of message on: fork off {topic}, fork-off {topic},
  fork {topic}, fork: {topic}. Never matches mid-sentence. Current session continues
  uninterrupted.
version: 1.0.0
---

# Fork-Off

Captures a tangent without ending the current session.

---

## Triggers

Positive (start of message only):
- `fork off {topic}`
- `fork-off {topic}`
- `fork {topic}`
- `fork: {topic}`

Negative:
- Mid-sentence: "let's fork off later" -- do NOT trigger
- Requests to end the session -- use handchat instead

---

## Step 1 -- Slugify topic

Lowercase. Replace non-alphanumeric with single dash. Strip leading/trailing dashes. Drop stopwords (the, a, an, and, or, of, to, for, in, on, at, with, by). Truncate to 40 chars.

Example: "fork off the new pricing model" → `new-pricing-model`

---

## Step 2 -- Build context capture

```
[{YYYY-MM-DD}]
Topic: {topic}
Last user turn: {trimmed to 400 chars}
Last assistant turn: {trimmed to 400 chars}
```

Total under 800 chars. If over, trim turns proportionally -- never trim header or topic.

---

## Step 3 -- Append to forks file

Write to `USER/routing/sessions.md` as a FORK entry:

```
### {YYYY-MM-DD} -- FORK: {slug}
**Topic:** {topic}
**Context:**
{context capture}
**Resume with:** pickup {slug}
```

### claude-code surface

```bash
source ~/.config/memory-starter/.env
BRANCH="${GITHUB_BRANCH:-main}"
FILE_PATH="USER/routing/sessions.md"

RESPONSE=$(curl -s -H "Authorization: Bearer ${GITHUB_TOKEN}" \
  "https://api.github.com/repos/${GITHUB_REPO}/contents/${FILE_PATH}?ref=${BRANCH}")
CURRENT=$(echo "$RESPONSE" | python3 -c "import sys,json,base64; d=json.load(sys.stdin); print(base64.b64decode(d['content']).decode())")
SHA=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['sha'])")

UPDATED="${CURRENT}

### {YYYY-MM-DD} -- FORK: {slug}
**Topic:** {topic}
**Context:**
{context_capture}
**Resume with:** pickup {slug}
"

ENCODED=$(echo "$UPDATED" | python3 -c "import sys,base64; print(base64.b64encode(sys.stdin.buffer.read()).decode())")

curl -s -X PUT "https://api.github.com/repos/${GITHUB_REPO}/contents/${FILE_PATH}" \
  -H "Authorization: Bearer ${GITHUB_TOKEN}" \
  -H "Content-Type: application/json" \
  -d "{\"message\": \"fork: {slug}\", \"content\": \"${ENCODED}\", \"sha\": \"${SHA}\", \"branch\": \"${BRANCH}\"}"
```

### claude-project surface

Produce the FORK entry block for the user to paste into sessions.md. Do not attempt HTTP calls.

---

## Step 4 -- Respond

On success, exactly one line:
```
forked: {slug} -- resume with: pickup {slug}
```

Then continue the current session as if nothing happened.

On failure:
```
fork failed -- write error. Copy this manually:
{full entry block}
```
