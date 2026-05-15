---
name: review-contributions
description: "Use when the user says \"review contributions\", \"what is pending\", or \"promote contributions\". Owner only."
when_to_use: "Use when the user says \"review contributions\", \"what is pending\", or \"promote contributions\". Owner only."
disable-model-invocation: true
version: 1.1.0
---

# review-contributions

## Trust

Reads: contributions/ inbox via GitHub API. Writes: USER/ canonical files on promote (GitHub PUT), contributions/_promoted/ archive (GitHub PUT), contributions/ inbox (GitHub DELETE on archive). External calls: GitHub API (GET, PUT, DELETE).

## Instructions

Load credentials:
```bash
source ~/.config/memory-starter/.env
# Expects: GITHUB_TOKEN, GITHUB_REPO (format: owner/repo)
```

### Step 1 — List pending contributions

```bash
curl -s "https://api.github.com/repos/${GITHUB_REPO}/contents/contributions" \
  -H "Authorization: Bearer ${GITHUB_TOKEN}" | \
  python3 -c "
import sys, json
files = json.load(sys.stdin)
pending = [f for f in files if isinstance(f, dict) and f['name'].endswith('.md') and not f['name'].startswith('_')]
for f in pending:
    print(f['name'])
"
```

### Step 2 — Fetch and display each file for review

```bash
curl -s "https://api.github.com/repos/${GITHUB_REPO}/contents/contributions/{FILENAME}" \
  -H "Authorization: Bearer ${GITHUB_TOKEN}" | \
  python3 -c "import sys,json,base64; d=json.load(sys.stdin); print(base64.b64decode(d['content']).decode())"
```

Present each to the owner. For each, ask: **Promote, Skip, or Discard?**

### Step 3 — On Promote

Move content to the appropriate USER/ canonical file (owner decides which). Then archive:

```bash
# Get SHA of original
SHA=$(curl -s "https://api.github.com/repos/${GITHUB_REPO}/contents/contributions/{FILENAME}" \
  -H "Authorization: Bearer ${GITHUB_TOKEN}" | python3 -c "import sys,json; print(json.load(sys.stdin)['sha'])")

# Copy to _promoted/
ENCODED=$(curl -s "https://api.github.com/repos/${GITHUB_REPO}/contents/contributions/{FILENAME}" \
  -H "Authorization: Bearer ${GITHUB_TOKEN}" | python3 -c "import sys,json; print(json.load(sys.stdin)['content'].replace('\n',''))")

curl -s -X PUT "https://api.github.com/repos/${GITHUB_REPO}/contents/contributions/_promoted/{FILENAME}" \
  -H "Authorization: Bearer ${GITHUB_TOKEN}" \
  -H "Content-Type: application/json" \
  -d "{\"message\": \"promote: {FILENAME}\", \"content\": \"${ENCODED}\"}"

# Delete from inbox
curl -s -X DELETE "https://api.github.com/repos/${GITHUB_REPO}/contents/contributions/{FILENAME}" \
  -H "Authorization: Bearer ${GITHUB_TOKEN}" \
  -H "Content-Type: application/json" \
  -d "{\"message\": \"archive: {FILENAME}\", \"sha\": \"${SHA}\"}"
```

### Step 4 — On Discard

Same as promote but skip the USER/ write. Move to `contributions/_promoted/` with a `discard:` commit message.

For claude-project surface: produce all curl commands above for the user to run. Do not attempt to execute them.
