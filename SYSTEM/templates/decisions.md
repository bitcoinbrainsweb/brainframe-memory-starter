# Decisions — {project-name}

**Class:** APPEND-ONLY
**Writer:** `quitchat` skill
**Purpose:** Historical log of decisions made. New entries at the bottom. Old entries are never edited.

On overflow (>12,000 chars): oldest entries archive to `decisions-archive/{project}-{YYYY-MM}.md`, keeping the last ~60 days active here.

## Entry format

```
### YYYY-MM-DD — Short title
**Status:** CONFIRMED / PROVISIONAL / SUPERSEDED (by: YYYY-MM-DD title)
**Context:** Why this came up.
**Decision:** What we decided.
**Reasoning:** Why this was the right call.
```

---

<!-- Append new decisions below. Do not edit old entries. -->

### 2026-01-01 — Example decision
**Status:** CONFIRMED
**Context:** Showing what a decision entry looks like.
**Decision:** Keep this format.
**Reasoning:** Structured, grep-able, survives file rotation.
