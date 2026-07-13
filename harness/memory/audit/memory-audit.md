# Memory Audit (worked example)

A documented example of a memory-audit routine. It audits stored memory entries
against an objective write gate, surfaces a verdict per entry, and applies only
what the operator approves. Adapt the gates to your own project; the value is the
shape (objective tests, verdict types, race-safe apply), not these specific checks.

This operates ONLY on your memory entries. It is distinct from a code security
review or a live-site scan.

---

## What this audits

Memory's role here is failsafe-only: cross-project rules that must survive even
when project instructions, global rules, and canonical files fail to load.
Anything else has a better home (a doc, a database row). For every memory entry,
this routine verifies it passes the write gate, flags what does not, surfaces
verdicts, and applies what the operator approves.

---

## Write gate (objective tests, in evaluation order)

Every entry must pass all six. First failure determines the verdict; remaining
gates are skipped.

### Gate 1 - Cross-project
Entry text contains zero project-specific tokens (your own project slugs and
internal upgrade-id prefixes). Exception: a token that appears only inside a
canonical-index file path does not count.
If it fails: `MOVE_TO_FILE: <routing path for that project>`

### Gate 2 - Not duplicated in canonical sources
Tokenize the entry and each canonical source into lowercased word-shingles of
length 8, compute Jaccard similarity. Threshold: >= 0.5 means duplicate.
Canonical sources are fetched fresh per audit (your global rules file, your
lookup/index file, your people file, and all installed skill descriptions).
If a canonical source cannot be fetched, the entry is marked
`KEEP-UNVERIFIED-DEDUP: <source>` and is never auto-removed; the operator decides.
If it fails: `REMOVE: duplicates <source> (Jaccard <score>)`

### Gate 3 - Behavior-changing
Entry must contain at least one of: an imperative verb in the first clause
(Reply, Fetch, Append, Never, Always, Use), a negation (Never X, Don't X), or a
conditional (When X, do Y). Auto-fail if the entry is purely informational
(starts "X is ...", lists facts/versions/dates with no "do this when" clause, or
restates a known status).
If it fails: `REMOVE: not behavior-changing`

### Gate 4 - Failsafe-worthy
Passes only if YES to all three: (a) a session with no project instructions and
no canonical-file access would still need this rule to avoid a concrete
safety/correctness failure; (b) the rule is cross-project; (c) no mechanism
outside memory already enforces it reliably. A pre-applied `[failsafe]` tag from
the operator is authoritative and skips (a) and (c): this is the deterministic
override for the one subjective gate.
If it fails: `REMOVE: not failsafe-worthy`

### Gate 5 - Not row-shaped
The entry would NOT fit better as a row in one of your state tables (roadmap
items, specs, decisions, open questions, blockers, sessions, state_events). A
"row-shaped" entry primarily tracks the STATE (in-progress, shipped, blocked,
active, resolved) of a specific named item.
If it fails: `MOVE_TO_STATE: <table>`

### Gate 6 - Concise wording
Entry text <= 280 characters.
If it fails: `TIGHTEN: <proposed shorter wording>` (suggest only; never auto-apply
a text change).

---

## Verdict types

| Verdict | Action when the operator approves apply |
|---|---|
| `KEEP` | No-op |
| `KEEP-UNVERIFIED-DEDUP: <source>` | No-op now; flag for next-session re-check |
| `REMOVE: <reason>` | Remove entry by content_hash (never by line number) |
| `MOVE_TO_FILE: <path>` | Append entry text to the file, commit, then remove the memory entry only after the write succeeds |
| `MOVE_TO_STATE: <table>` | Insert a row, then remove the memory entry only after the insert succeeds |
| `TIGHTEN: <wording>` | Replace by content_hash with the proposed wording (operator reviews first) |

For any MOVE_TO_STATE, if the entry does not supply enough text to fill the
table's required columns, the verdict downgrades to `MANUAL`.

---

## Snapshot + apply discipline (race-safe)

Line numbers are not safe if memory changes between snapshot and apply. Resolve
every apply by content hash instead:

1. Snapshot memory into `{line, content, content_hash}` where
   `content_hash = sha256(content)[:16]`.
2. Compute verdicts on the snapshot.
3. Before each individual apply, re-snapshot and find the target by content_hash.
   If the hash is missing or duplicated, abort that apply with `STALE_SNAPSHOT`,
   surface it, and continue to the next.
4. Apply removes highest-line-first, then replaces, then moves last (moves also
   write to an external system).

No verdict is auto-applied without approval. No memory entry is removed before
its target write succeeds.

---

## Steps

1. Mint a run id `<ISO-date>-<short-uuid>` and snapshot memory.
2. Fetch dedup sources; mark any unreachable source UNFETCHED (its entries become
   KEEP-UNVERIFIED-DEDUP).
3. Evaluate gates per entry, first-failure-wins.
4. Surface an inline report table: `# | Hash | Verdict | Reason`.
5. Prompt: `apply N,M / apply all / per-fix / skip`.
6. Apply per the discipline above.
7. Append one line per apply to the append-only audit log
   (see `audit-log.template.md`): never mix this into your architecture-decisions
   log.

---

## Failure modes

Every failure degrades safely: an unreachable dedup source downgrades to
KEEP-UNVERIFIED-DEDUP, a failed external write downgrades to MANUAL and leaves the
memory entry in place, a stale snapshot skips only that one apply. The routine
never blocks a session and never removes an entry whose target write did not
succeed.
