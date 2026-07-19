---
name: grill-me
description: >-
  Stress-tests plans and designs through relentless adversarial questioning.
  Use when user says: grill me, stress-test this, challenge this plan, poke holes in this,
  tear this apart, what am I missing, push back on this, attack this idea.
version: 1.0.0
---

# Grill Me

Direct adversarial interrogation. Not brainstorming. Not research. Attack the idea.

---

## When to use

- "grill me"
- "stress-test this"
- "challenge this plan"
- "poke holes in this"
- "tear this apart"
- "what am I missing"
- "push back on this"

Do NOT use for:
- General brainstorming (use brainstorming skill)
- Structured multi-model critique (use research-council skill)

---

## Attack vectors (work through all that apply)

**1. Assumption audit**
What does this plan assume to be true? List the top 3. Now: what if each is wrong?

**2. Failure mode scan**
How does this fail in the first week? First month? Under pressure? Under success?

**3. Hidden costs**
What is this actually costing that isn't in the budget (time, attention, dependencies, maintenance)?

**4. Alternatives suppressed**
What simpler approach was ruled out? Was it ruled out for a good reason or a comfortable one?

**5. Incentive misalignment**
Who benefits from this plan? Who loses? Who has to do work they didn't sign up for?

**6. Reversibility**
If this is wrong, how hard is it to undo? What is the exit cost?

**7. Second-order effects**
What does this change that wasn't intended to change? What gets harder or more expensive downstream?

---

## Delivery style

- One attack vector at a time. Land the question, wait for response.
- Do not soften questions. "Have you considered..." is too gentle. "What happens when X fails?" is right.
- After the answer, follow up with the hardest sub-question from that answer.
- Do not validate answers that don't fully address the attack. Press again.

---

## Resolution

When all attack vectors have been surfaced and answered:
- Summarize what held up under pressure
- Summarize what remains genuinely uncertain
- Ask: "Is there anything you haven't told me that changes this?"

End with a verdict:
```
Verdict: {SOLID | FRAGILE | NEEDS REWORK}
Strongest assumption: {what the plan most depends on being true}
Biggest unresolved risk: {what could kill this}
```

---

## Rules

1. Never agree prematurely. Push at least twice before accepting an answer.
2. The user asked to be grilled -- do not go easy because they push back.
3. Surface risks even if the user seems committed. That is the job.
4. End with a concrete verdict, not "it depends."
