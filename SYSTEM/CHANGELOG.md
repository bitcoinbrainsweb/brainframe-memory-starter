# Changelog

## v1.0.0 — 2026-04-17

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
- Tier 2 upgrade path (self-hosted Graphiti for Wire 1/2)
- Automated template-sync PRs from upstream master repo
- Additional language support beyond English

### Known limitations
- Private forks require uploading routing files to Claude Project Knowledge (web_fetch can't access private repos)
- GitHub API rate limit applies at Tier 0 (no caching) — 5000 req/hr authenticated, plenty for normal use
- No event log at Tier 0 — drift detection is manual / quarterly human review
