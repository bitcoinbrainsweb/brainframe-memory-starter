# Advisor System

Lets you invoke named advisors by slug to run their mental models, heuristics, and challenge questions against any decision or artifact.

## Structure

```
advisors/
  INDEX.md            Active advisor roster -- lazy-fetched on first use each session
  README.md           This file
  _template.md        Copy to create a new advisor
  strategic/          Frameworks, positioning, long-game
  tactical/           Execution, offers, negotiation, copy
  thinker/            First principles, worldview, philosophy
  finance/            Capital, risk, burn, fundability
```

## How to invoke

- "ask [name] about X"
- "run this through [name]"
- "what would [name] say"
- "[name] on this"
- Multi-advisor: "ask [name1] and [name2] about X"

## How to add an advisor

1. Copy `_template.md` to `[category]/[slug].md`
2. Fill in corpus, mental model, mandatory steps, heuristics
3. Add a row to `INDEX.md` with `status: active`

## How Claude uses these files

1. Parse the advisor slug from the invocation
2. Fetch `INDEX.md` (once per session, cached after)
3. Resolve slug to file path and status
4. If status=pending: list active advisors and stop
5. Fetch the advisor file
6. Run mandatory steps against the current artifact or question
7. Output: mandatory step results, heuristics that apply, at least one challenge question
