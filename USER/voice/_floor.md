# Universal Slop Floor

**Version:** v1.1
**Authority:** repo owner PR and approval to modify

This file is the floor. Every ban applies to every output across every surface, every author, every doc type. Author signature files cannot override these.

## How to run the floor check

Run the check as code, not by eye. After drafting, grep the output against the patterns below; rewrite every hit; re-check; repeat until clean. One pass never suffices. Claiming a floor pass ran without grepping is a banned behavior.

## Absolute Bans

- **Em-dashes (`--` or `---`, U+2014, U+2013 as em-dash):** Use comma, semicolon, colon, or period instead.
- **Parallel-negation triplet ("No X. No Y. No Z."):** Collapse to a single clause. "No A, B, or C." is fine.
- **Contrastive-frame inversion ("it's not X, it's Y" / "isn't X, it's Y" / "X is not Y. It is Z."):** State the affirmative directly, or carry the contrast across a wider span so the inversion is not back-to-back. This is the single most common AI tell; it is the highest-priority catch.
- **Throat-clearing openers** ("I hope this finds you well", "In today's fast-paced world"): cut.
- **"It's important to note that":** cut.
- **Adjective stacking** ("innovative, cutting-edge, transformative"): pick one or none.

### Mechanical patterns for the contrastive-frame catch

Grep these (case-insensitive); any hit must be rewritten:
- `(is|are|was|were)\s+not\s+\w[^.]*[,.]\s*(it|that|they)('?s|\s+is|\s+are)`
- `(isn't|aren't|wasn't|weren't|doesn't|don't|didn't)\s+\w[^.]*[,.]\s*(it|that|they)('?s|\s+is|\s+are|\s+but)`
- `not\s+(just|merely|only|about|because)\s+\w[^.]*[,.]\s*(it|that|but|they)`
- `\b(is|are|does|do|did)\s+not\s+[^.]*\.\s+(It|That|They)\b`

## Banned Words

Verbs: utilize (say "use"), leverage (as a verb), facilitate, showcase, streamline, revolutionize, transform, empower, unlock, underscore, delve, explore.

Adjectives: robust, seamless, comprehensive, cutting-edge, innovative, transformative, impactful.

Nouns (filler): ecosystem, landscape, paradigm, synergy, journey, tapestry.

Adverbs: importantly, notably, crucially, significantly (when unquantified).

## Banned Structural Patterns

- Opening an email or post with "I" as the first word
- Bullet lists inside prose contexts unless structure genuinely helps
- More than one exclamation point per document
- Passive voice when active works

## Strip Test

Before delivering: could this sentence have been written by a competent human who cares about the topic? If no, rewrite it.
