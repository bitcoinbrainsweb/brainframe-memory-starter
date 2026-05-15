---
name: research-council
description: >-
  Runs multi-model critique on specs, plans, or decisions. Use when user says:
  research council, run the council, council this, critique this spec, get me a
  second opinion on this. Produces structured critique with verdict and unresolved risks.
version: 1.0.0
---

# Research Council

Structured multi-perspective critique. Use before committing to irreversible decisions.

---

## When to use

- "research council"
- "run the council"
- "council this"
- "critique this spec"
- "get me a second opinion"
- Before marking a spec ACTIVE from DRAFT on an architectural decision

Do NOT use for:
- General brainstorming (use brainstorming skill)
- Direct adversarial pressure (use grill-me skill)

---

## What this produces

A structured critique covering:
1. What the plan gets right
2. What it gets wrong or underspecifies
3. What it assumes that may not hold
4. What the unresolved risks are
5. A verdict: ACCEPT | REVISE | REWORK

---

## Council structure

The council runs two paths:

**Parallel path:** Three independent critics read the spec cold and produce verdicts without seeing each other's output.

**Sequential path:** One critic reads, produces critique, second critic reads both spec and first critique, third critic reads all prior output. Each builds on the last.

After both paths: merge findings, deduplicate, surface the delta (what the sequential path caught that parallel missed, and vice versa).

---

## Running the council (claude-code surface)

The council requires API keys for at least two different models. Load from env:

```bash
source ~/.config/memory-starter/.env
# Expects one or more of:
# ANTHROPIC_API_KEY, OPENAI_API_KEY, PERPLEXITY_API_KEY
```

**Minimum viable council (1 key — Anthropic only):**

Run 3 separate prompts against claude-sonnet with varied critic personas:

```bash
SPEC_CONTENT=$(cat /path/to/spec.md)

for PERSONA in "skeptical engineer" "product manager who has seen this fail before" "security reviewer"; do
  curl -s "https://api.anthropic.com/v1/messages" \
    -H "x-api-key: ${ANTHROPIC_API_KEY}" \
    -H "anthropic-version: 2023-06-01" \
    -H "Content-Type: application/json" \
    -d "{
      \"model\": \"claude-sonnet-4-20250514\",
      \"max_tokens\": 1000,
      \"messages\": [{
        \"role\": \"user\",
        \"content\": \"You are a ${PERSONA}. Critique this spec. Be direct. Identify the 3 biggest risks. End with ACCEPT, REVISE, or REWORK.\n\n${SPEC_CONTENT}\"
      }]
    }" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['content'][0]['text'])"
  echo "---"
done
```

**Full council (multiple keys):** Add OpenAI and Perplexity calls alongside Anthropic for genuine model diversity. Same prompt structure, different endpoints.

---

## Running the council (claude-project surface)

Produce the critique prompts for each persona and ask the user to run them against their preferred tools (ChatGPT, Claude, Perplexity). Collect results and synthesize.

---

## Synthesis

After all critics have run, produce:

```
## Council Summary

**Consensus risks:**
- {risk that multiple critics flagged}

**Divergent findings:**
- Parallel path caught: {X}
- Sequential path caught: {Y}

**What held up:**
- {element that survived all critique}

**Unresolved:**
- {genuine uncertainty no critic resolved}

**Verdict:** ACCEPT | REVISE | REWORK
**Reasoning:** {one sentence}
```

---

## Rules

1. Minimum 2 critic passes before synthesis. 1 pass is not a council.
2. Verdict is mandatory — do not end with "it depends."
3. ACCEPT means proceed as-is. REVISE means fix specific items before proceeding. REWORK means fundamental issues.
4. Unresolved risks survive the council — do not paper over genuine uncertainty with a confident verdict.
