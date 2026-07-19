---
name: brainstorming
description: >-
  Explores feature design through Socratic dialogue before implementation begins.
  Use when user says: how should we build, thinking about adding, design for,
  what if we, brainstorm. Do NOT use when requirements are already defined.
version: 1.0.0
---

# Brainstorming

Explore before committing. This skill deliberately avoids converging too fast.

---

## When to use

- "how should we build X"
- "thinking about adding X"
- "design for X"
- "what if we X"
- Any ambiguous build request before requirements exist

Do NOT use when:
- Requirements are already defined and approved (use spec-writing)
- A decision has already been made (use adr)

---

## The three questions

Before diving into solutions, ask at most three clarifying questions:

1. **Who is this for?** User, system, external API, internal admin?
2. **What does success look like?** One concrete outcome that would make this worth building.
3. **What have you already ruled out?** Constraints the solution must respect.

Ask all three at once, not one at a time.

---

## Exploration mode

After answers, generate 2-3 distinct approaches. For each:
- Name it (memorable, not generic)
- State the core bet it makes
- List the main tradeoff

Format:
```
**Option A -- {Name}**
Bet: {what this approach assumes to be true}
Tradeoff: {what you give up}

**Option B -- {Name}**
Bet: {what this approach assumes to be true}
Tradeoff: {what you give up}
```

---

## Socratic pressure

After presenting options, do not wait passively. Push:
- "Which tradeoff can you live with?"
- "What would make Option A wrong?"
- "Is the bet in Option B actually true for your situation?"

The goal is to surface hidden assumptions, not to validate the user's first instinct.

---

## When to stop

When the user converges on an approach and can articulate why, offer to hand off:
```
Ready to spec this? I can write a formal spec now.
```

Do not write the spec inside brainstorming -- hand off to spec-writing skill.

---

## Rules

1. Never present more than 3 options -- more creates paralysis.
2. Always name each option distinctly -- "Option A / B / C" is not enough.
3. Push back at least once before accepting convergence.
4. Brainstorming ends with a hand-off to spec-writing, not with an implementation plan.
