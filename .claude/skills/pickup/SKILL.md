---
name: pickup
description: >-
  Resumes a paused thread. Use when user says: pickup {slug}, continue {slug},
  resume {slug}. Finds the matching HANDCHAT or FORK entry in sessions.md, surfaces
  context, executes immediately. No preamble. No re-asking.
version: 1.1.0
---

# Pickup -- Resume From Handchat or Fork

Trigger: `pickup` + any words. Fuzzy match always.

Two skills write resumable entries and both point here: `handchat` writes
`HANDCHAT:` entries, `fork-off` writes `FORK:` entries. Match on either.

---

## Step 1 -- Find the entry

Scan `USER/routing/sessions.md` (and project sessions files if a project is active) for a `HANDCHAT:` or `FORK:` entry matching the slug words.

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
# Find resumable entries: handchat pauses and fork-off tangents
entries = [e for e in text.split('### ') if 'HANDCHAT:' in e or 'FORK:' in e]
for e in entries[-5:]:
    print('###', e[:200])
"
```

### claude-project surface

Read the sessions.md content from context (fetched at boot). Find HANDCHAT and FORK entries.

---

## Step 2 -- Fuzzy match

Join slug words with `-` and find the entry whose slug contains those words. If multiple match, list them and ask user which. If one matches, proceed.

If no match: list all HANDCHAT and FORK entries from sessions.md and ask user to pick.

---

## Step 3 -- Surface and execute

For a `HANDCHAT:` entry, print:
```
Picking up: {slug}

{context_brief -- mental model section}

Last: {last action from next_action}
Next: {pick up here from next_action}
```

For a `FORK:` entry there is no next_action; the entry holds a topic and two trimmed turns. Print:
```
Picking up fork: {slug}

Topic: {topic}
Where it was left: {context capture}
```

Then **immediately execute** "pick up here" -- no confirmation, no preamble. On a fork, the topic is the task: open it, do not ask the user to restate it.

---

## Rules

1. Fuzzy match always -- "pickup api rate thing" works fine.
2. Match HANDCHAT and FORK entries alike. A fork the user cannot resume is a fork that was never captured.
3. Execute immediately after surfacing context.
4. Never say "shall I proceed."
5. If no match: list recent HANDCHAT and FORK entries, never just say "not found."
