# Changelog

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
- Six-layer memory model (L0–L5)
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
