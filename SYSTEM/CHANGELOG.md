# Changelog

## v1.3.0 -- 2026-08-14

Shipping fix plus a rule-maturation pass. Backward compatible.

### Fixed
- `.claude/skills/quitchat/SKILL.md` shipped as a 0-byte file, so every fork got a dead skill with no frontmatter and no description. Written from scratch: self-contained, structurally consistent with `handchat` and `pickup`, references only repo-local surfaces (`USER/routing/sessions.md`, `harness/memory/audit/memory-audit.md`, `harness/sessions/SCHEMA.sql`).
- `pickup` could not resume a `fork-off` tangent. `fork-off` writes `FORK:` entries and tells the user to resume with `pickup {slug}`, but `pickup` matched `HANDCHAT:` entries only. It now matches both, and prints a fork-shaped surface for fork entries.
- `SYSTEM/LOOKUP.md` writer rules were wrong: they gave `decisions.md` to `quitchat` and `facts.md` to `adr`. The shipped skills do the opposite. Replaced with a writer table that matches the skills.
- `research-council` pinned a stale model id in its example call. Replaced with a `COUNCIL_MODEL` variable and a note that pinned ids go stale.
- `prompt-writing` delivered to a hardcoded `/tmp` path, which does not exist on Windows forks.
- `scripts/check-strip.sh` could never pass: it listed the kit's own `YOUR_GITHUB_USER` placeholders as forbidden terms. Placeholders are intended content and are no longer flagged.
- Residual private-plan references in five advisor files (an unnamed business, a jurisdiction-specific entity structure, a specific burn figure and revenue date) that the v1.2.1 sanitization pass missed. Genericized.
- One U+2013 en-dash in this changelog.

### Added
- `GLOBAL_RULES` > `Cost`: warn with an estimate before metered spend, report actuals afterward including overruns.
- `GLOBAL_RULES` > `Agent dispatch`: every dispatch prompt declares depth, fan-out, and respawn policy. Default depth 1, fan-out 2, no poll-and-respawn. Wait against a wall-clock cap; never respawn on timeout.
- `GLOBAL_RULES` > `Maintenance cadence`: re-run the harness audit on a major model release, and after the same novel failure recurs. Triggers only; `docs/agent-guides/maintenance.md` holds the checklist.
- `harness/foreman/SETUP.md` > `Dispatch rules`: the bundle is the unit of dispatch, every dispatch goes through the orchestrator, one issue one attempt. Orchestration rules, deliberately kept out of GLOBAL_RULES.
- `SYSTEM/LOOKUP.md` > `On demand (T3)`: the on-demand routing tier that `CLAUDE.md` promised but LOOKUP never listed (topic notes, voice files, advisor files, harness setup docs).

### Changed
- `GLOBAL_RULES` > `Formatting` now names itself the single home for the em-dash ban. `USER/voice/_floor.md` is labelled as its mechanical check rather than a second rule, and `human-writing` points at both instead of restating.
- `prompt-writing` requires a Dispatch limits section in every prompt it writes, so its output satisfies the new agent-dispatch rule.
- `research-council` declares its cost estimate before running and its dispatch limits (depth 1, fan-out 3, no respawn).
- `harness/sessions/skills/quitchat.md` now states its relationship to the vendored copy: database counterpart, same orchestration order.

### Skill versions
`pickup`, `prompt-writing`, and `research-council` move to `1.1.0` (behaviour changed). `human-writing` stays at `1.1.0`. Every other vendored skill stays at `1.0.0`.

## v1.2.1 -- 2026-07-19

- Sanitization pass: advisor Example application sections replaced with generic templates (internal strategy content removed)
- Em-dash style ban enforced repo-wide (double hyphen convention)
- Internal project reference removed from foreman conformance docstring
- Sanitization verified by full-tree read-back scan

## v1.2.0 -- 2026-06-15

Consolidation and rule-hardening pass. Backward compatible (older forks keep working via redirect stubs).

### Changed
- Completed the TIER_A/TIER_B -> GLOBAL_RULES/LOOKUP rename. `SYSTEM/TIER_A.md` and `SYSTEM/TIER_B.md` are now redirect stubs.
- Routing tables moved to `SYSTEM/LOOKUP.md` (new).
- `GLOBAL_RULES.md` expanded with matured universal rules:
  - Truth and verification: "measure or admit" anti-hallucination rule; FACT / INFERENCE / UNKNOWN claim categorization.
  - Memory: explicit canonical-file precedence (files win; ambient memory is a lossy pointer).
  - Decision states: CONFIRMED / PROVISIONAL / SUPERSEDED.
  - Graceful degradation chain (T3 -> T2 -> T1 fall-through; name the gap).
  - Spec hygiene (existence check before drafting).
  - Credential live-verification before delivery.
  - Voice pipeline is mandatory and un-fakeable.
  - No-narration of tool use.
  - Optional boot/degradation canary.
- `PROJECT_INSTRUCTIONS.md` boot sequence updated to fetch GLOBAL_RULES + LOOKUP + PROJECTS + people, with a four-line memory gate and graceful degradation.
- `VERSION` and `CLAUDE.md` reconciled to v1.2.0 (the half-shipped v1.1.0 version line is rolled into this release).
- Voice floor bumped to v1.1: mechanical grep discipline plus explicit contrastive-frame patterns.

### Notes
- The four-file files-only L3 model (facts / preferences / decisions / sessions) remains the default entry tier. Supabase stays optional.

## v1.0.0 -- 2026-04-17

Initial release. Tier 0 (files-only) feature-complete.

### Shipped
- Six-layer memory model (L0 to L5)
- USER/SYSTEM split with `.templatesyncignore` protecting `USER/**`
- Four-file L3 per project: facts (IMMUTABLE), preferences (MUTABLE), decisions (APPEND-ONLY), sessions (ROLLING)
- Refresh grammar: `000`, `001`, `002`, `@project`, `recall:`
- Example project pre-populated under `USER/routing/example-project/`
- Paste-in `PROJECT_INSTRUCTIONS.md` for Claude Projects
- Graceful degradation: any layer fetch failure falls through without halting
- Bootstrap flow for fresh forks and new projects

### Not in this release (on roadmap)
- Tier 1 upgrade path (Supabase event log, health reports, drift detection)
- Tier 2 upgrade path (self-hosted vector memory layer)
- Automated template-sync PRs from upstream master repo
- Additional language support beyond English

### Known limitations
- Private forks require uploading routing files to Claude Project Knowledge (web_fetch can't access private repos)
- GitHub API rate limit applies at Tier 0 (no caching) -- 5000 req/hr authenticated, plenty for normal use
- No event log at Tier 0 -- drift detection is manual / quarterly human review
