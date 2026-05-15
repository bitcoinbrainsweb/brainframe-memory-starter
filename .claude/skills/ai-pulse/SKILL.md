---
name: ai-pulse
description: >-
  Structured AI intelligence report from curated sources. Use when user says:
  ai pulse, daily report, what's new in AI, scan AI news, update me on AI.
  Covers models, tools, research, and business moves. Delivers signal, not noise.
version: 1.0.0
---

# AI Pulse

Structured intelligence on what matters in AI right now.

---

## When to use

- "ai pulse"
- "daily report"
- "what's new in AI"
- "scan AI news"
- "update me on AI"
- "what dropped"

---

## Source tiers

**Tier 1 — Must check (highest signal):**
- https://www.anthropic.com/news
- https://openai.com/news
- https://deepmind.google/discover/blog/
- https://x.ai/news
- https://mistral.ai/news/
- https://www.deepseek.com/

**Tier 2 — Check if time permits:**
- https://huggingface.co/blog
- https://www.together.ai/blog
- https://groq.com/news/
- https://simonwillison.net (practitioner commentary)
- https://www.latent.space/

**Tier 3 — Business and market:**
- https://techcrunch.com/category/artificial-intelligence/
- https://www.theinformation.com (paywalled — skim headlines only)

---

## Report format

```
## AI Pulse — {YYYY-MM-DD}

### Models
{New model releases, capability updates, benchmark results worth noting}

### Tools and infrastructure
{New dev tools, APIs, open-source releases}

### Research
{Papers or findings with practical implications — skip purely academic unless genuinely significant}

### Business and market
{Funding, acquisitions, partnerships, regulatory moves}

### Signal this week
{1-2 sentences: the most important thing that happened and why it matters}
```

---

## Process

1. Fetch Tier 1 sources. Extract items from the last 7 days (or since last pulse if user tracks cadence).
2. For each item: one sentence on what happened, one sentence on why it matters. Drop items where the second sentence would be "unclear" or "too early to say" — that's noise.
3. If a source is unreachable, note it and continue — do not block the report.
4. Write the report. No bullet walls — short prose paragraphs per section.
5. Signal this week is mandatory. If nothing clear emerges, say so explicitly.

---

## Quality gates

- No "it remains to be seen" — if it does, skip the item
- No items older than 14 days unless genuinely significant
- No reprinting press release language — translate to plain signal
- If a section has nothing worth reporting, write "{Section}: nothing significant this week" — do not omit sections

---

## Rules

1. Signal over completeness. Five sharp items beat twenty mediocre ones.
2. The report is for action, not awareness. Frame items in terms of what changes for someone building with AI.
3. One "Signal this week" item only — the most important thing, not a list.
4. Deliver as a complete report, not a summary of summaries.
