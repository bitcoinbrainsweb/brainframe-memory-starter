---
name: advisor
description: >-
  Invokes a named advisor from advisors/INDEX.md to run their mental models,
  heuristics, and challenge questions against the current problem or artifact.
  Trigger phrases: "ask [name]", "run this through [name]", "what would [name] say",
  "[name] on this". Multi-advisor: any of the above with multiple names.
version: 1.0.0
---

# Advisor

## Purpose

Invoke any advisor by name to run their mandatory steps, decision heuristics, and challenge questions against the current problem -- then deliver a structured verdict.

## Triggers

### Positive
- "ask [advisor name]" -- e.g. "ask Ogilvy", "ask Hormozi about this"
- "run this through [name]" -- e.g. "run this through Voss"
- "what would [name] say" -- with a current artifact or decision in context
- "apply [name] to this" -- e.g. "apply Ogilvy to this headline"
- "[name] on this" -- e.g. "Ogilvy on this copy"
- Multi-advisor: any of the above with multiple names

### Negative
- Past-tense rhetorical with no current decision artifact
- "ask an advisor" with no name named -- surface index, ask user to pick; do not run
- General research questions about the advisor's published views (just answer from training)

## Steps

### Step 1 -- Trigger classification

Identify which advisor(s) are named. Match against slug or full name in INDEX.md (case-insensitive). If ambiguous, list candidates and stop.

If no advisor named: fetch INDEX.md, list active advisors, ask user to name one. Stop.

### Step 2 -- INDEX.md fetch (lazy, once per session)

If not yet fetched this session: fetch `advisors/INDEX.md` from repo and hold in session memory.

### Step 3 -- Slug resolution

Resolve named advisor to slug and file path via INDEX.md.

If not found: output "Advisor '[name]' not found." + list all active slugs. Stop.

If status=pending: output "Advisor '[slug]' is pending -- not yet filled in. Active advisors: [list]." Stop.

### Step 4 -- Advisor file fetch

Fetch `advisors/[category]/[slug].md` from repo.

### Step 5 -- Run mandatory steps

Execute every step listed in the advisor file's "Mandatory steps" section against the current problem. Output results for each step explicitly.

### Step 6 -- Apply heuristics

Identify which of the advisor's decision heuristics apply to the current situation. Surface the 2-4 most relevant, with source references.

### Step 7 -- Challenge

Deliver at least one challenge question from the advisor's perspective. If multi-advisor, each advisor poses their own challenge.

## Output format

```
## [Advisor Name] on [topic]

**Mandatory steps**
[Step results]

**Heuristics that apply**
[2-4 heuristics]

**Challenge**
> "[Question]"
```

Multi-advisor: run each advisor independently, then stop. Synthesis only if explicitly requested.

## Trust

This skill reads from the repo (GitHub API) using the repo PAT. It does not write. No confirmation needed before fetching.
