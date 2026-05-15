---
name: spec-writing
description: >-
  Writes structured specs for features, systems, or decisions. Use when user says:
  spec this, write a spec, stub this, draft the spec, turn this into a spec.
  Produces EARS-style requirements with stable IDs. Routes large architectural
  decisions through research-council.
version: 1.0.0
---

# Spec Writing

---

## When to use

- "spec this" / "write a spec" / "stub this" / "draft the spec"
- After brainstorming when requirements are clear enough to formalize
- Before implementation begins on any non-trivial feature

Do NOT use for:
- Prompt writing (use prompt-writing skill)
- Recording decisions already made (use adr skill)

---

## Spec tiers

| Tier | When | Output |
|------|------|--------|
| Stub | Idea is early, scope unclear | Title + problem + 3-5 bullet requirements |
| Full | Scope is clear, ready for build | Full EARS requirements with IDs |
| ADR | Irreversible architectural choice | Full spec + route through research-council |

Default to Full unless the idea is clearly early-stage.

---

## Step 1 — Existence check

Before writing, ask: does a spec for this already exist? Check `USER/routing/{project}/facts.md` and `USER/topics/` for any matching entry. If found, surface it and ask whether to extend or replace.

---

## Step 2 — Draft

**Header:**
```
# {Title}
Status: DRAFT | ACTIVE | SUPERSEDED
Version: 1.0
Date: {YYYY-MM-DD}
Project: {project or "general"}
```

**Problem statement** (2-3 sentences): what problem does this solve and for whom.

**Requirements** (EARS format):

Use stable IDs: `R1`, `R2`, etc. Each requirement gets acceptance criteria: `R1.AC1`, `R1.AC2`.

EARS patterns:
- Ubiquitous: `The system SHALL {action}`
- Event-driven: `WHEN {trigger} THE system SHALL {action}`
- Unwanted: `IF {condition} THEN the system SHALL NOT {action}`
- Optional: `WHERE {feature} is available THE system SHALL {action}`

Example:
```
R1. The system SHALL write session summaries at close via quitchat.
  R1.AC1. Summary includes worked-on, decisions, open items, and next action.
  R1.AC2. Entry is appended to the active project's sessions.md.

R2. WHEN a spec already exists for the topic, the system SHALL surface it before writing a new one.
```

**Open questions** (bulleted, if any)

**Out of scope** (bulleted, if any)

---

## Step 3 — Save

Save to `USER/topics/{slug}.md`.

### claude-code surface

```bash
source ~/.config/memory-starter/.env
BRANCH="${GITHUB_BRANCH:-main}"
SLUG="{spec-slug}"
FILE_PATH="USER/topics/${SLUG}.md"

ENCODED=$(cat /tmp/spec_draft.md | python3 -c "import sys,base64; print(base64.b64encode(sys.stdin.buffer.read()).decode())")

curl -s -X PUT "https://api.github.com/repos/${GITHUB_REPO}/contents/${FILE_PATH}" \
  -H "Authorization: Bearer ${GITHUB_TOKEN}" \
  -H "Content-Type: application/json" \
  -d "{\"message\": \"spec: add ${SLUG}\", \"content\": \"${ENCODED}\", \"branch\": \"${BRANCH}\"}"
```

### claude-project surface

Produce the full spec as markdown for the user to save to `USER/topics/{slug}.md`.

---

## Step 4 — ADR tier routing

If the spec represents an irreversible architectural choice (data model, auth approach, external dependency, storage format), flag it:

```
This spec involves an irreversible architectural decision.
Recommend: run research-council before marking ACTIVE.
```

Ask user to confirm before proceeding to ACTIVE status.

---

## Rules

1. Requirement IDs are stable — never renumber after assignment.
2. Status progresses: DRAFT → ACTIVE → SUPERSEDED. Never skip DRAFT.
3. One spec per file, one file per spec.
4. Stubs are valid deliverables — not every spec needs to be full before being useful.
