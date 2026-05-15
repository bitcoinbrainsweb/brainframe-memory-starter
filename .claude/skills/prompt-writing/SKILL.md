---
name: prompt-writing
description: >-
  Writes production-quality prompts for Claude Code, Cursor, GPT, Perplexity, or
  any AI tool. Use when user says: write a prompt for, prompt this, give me a prompt,
  prompt for Claude Code, Cursor prompt. Delivers a clean file, not inline text.
version: 1.0.0
---

# Prompt Writing

Writes prompts for AI tools. Does not execute them.

---

## When to use

- "write a prompt for X"
- "prompt this"
- "give me a prompt"
- "Claude Code prompt for X"
- "Cursor prompt for X"

Do NOT use for:
- Writing specs (use spec-writing)
- Writing skills (use skill authoring pattern)

---

## Step 1 — Self-execution check

Before writing a prompt: can this task be resolved in-session right now?

If yes: do it directly. Do not write a prompt for something you can do yourself.

If no (task requires: a running codebase, multi-file edits, terminal access, a separate AI tool): write the prompt.

---

## Step 2 — Classify target tool

| Tool | When |
|------|------|
| Claude Code | Autonomous multi-file builds, DB migrations, long agentic tasks |
| Cursor | Single-file or focused codebase edits with human in loop |
| GPT / o3 | Critique, research, second opinion, structured analysis |
| Perplexity | Current events, real-time research, source-backed answers |

State the target tool at the top of the prompt.

---

## Step 3 — Prompt structure

Every prompt must include:

**Target:** {tool name and model if relevant}

**Run in:** {where to paste/run this — e.g. "Claude Code CLI in repo X", "Cursor chat", "ChatGPT with o3"}

**Context:** What the codebase/project is. What has already been done. What must not change.

**Task:** Exactly what to build or change. Specific, not vague.

**Constraints:**
- What to avoid
- What patterns to follow
- What files not to touch

**First step:** The very first thing the agent should do (e.g. `git checkout main && git pull && git checkout -b branch-name`).

**Done when:** Clear completion criteria. What does success look like?

---

## Step 4 — Quality gates

Before delivering, check:

- [ ] Task is specific enough that a competent agent could execute without asking clarifying questions
- [ ] First step is explicit (branch creation, file read, etc.)
- [ ] Constraints rule out the most likely failure modes
- [ ] "Done when" is verifiable, not subjective

---

## Step 5 — Deliver

For claude-code surface: write to `/tmp/{slug}-prompt.md` and present as a file.

For claude-project surface: deliver as a fenced markdown block the user can copy.

Always state where to run it.

---

## Rules

1. Never paste prompts inline in a long response — deliver as a file or copy block.
2. Always state the run location explicitly.
3. The best prompt is the shortest one that fully specifies the task.
4. If you can do the task yourself in this session, do it — don't write a prompt for it.
