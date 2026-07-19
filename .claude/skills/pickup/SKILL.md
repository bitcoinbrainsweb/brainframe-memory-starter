---
name: pickup
description: >-
  Resumes a handchat session. Use when user says: pickup {slug}, continue {slug},
  resume {slug}. Finds the handchat entry in sessions.md, surfaces context, executes
  immediately. No preamble. No re-asking.
version: 1.0.0
---

# Pickup -- Resume From Handchat

Trigger: `pickup` + any words. Fuzzy match always.

---

## Step 1 -- Find the handchat entry

Scan `USER/routing/sessions.md` (and project sessions files if a project is active) for a `HANDCHAT:` entry matching the slug words.

### claude-code surface

```bash
source ~/.config/memory-starter/.env
BRANCH="${GITHUB_BRANCH:-main}"
FILE_PATH="USER/routing/sessions.md"

curl -s -H "Authorization: Bearer ${GITHUB_TOKEN}" \
  "https://api.github.com/repos/${GITHUB_REPO}/contents/${FILE_PATH}?ref=${BRANCH}" | \
  python3 -c "
import sys,json,base64
d=json.load(sys.stdin)
text = base64.b64decode(d['content']).decode()
# Find HANDCHAT entries
entries = [e for e in text.split('### ') if 'HANDCHAT:' in e]
for e in entries[-5:]:
    print('###', e[:200])
"
```

### claude-project surface

Read the sessions.md content from context (fetched at boot). Find HANDCHAT entries.

---

## Step 2 -- Fuzzy match

Join slug words with `-` and find the entry whose slug contains those words. If multiple match, list them and ask user which. If one matches, proceed.

If no match: list all HANDCHAT entries from sessions.md and ask user to pick.

---

## Step 3 -- Surface and execute

Print:
```
Picking up: {slug}

{context_brief -- mental model section}

Last: {last action from next_action}
Next: {pick up here from next_action}
```

Then **immediately execute** "pick up here" -- no confirmation, no preamble.

---

## Rules

1. Fuzzy match always -- "pickup api rate thing" works fine.
2. Execute immediately after surfacing context.
3. Never say "shall I proceed."
4. If no match: list recent HANDCHAT entries, never just say "not found."
