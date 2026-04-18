# Skill Bundle Manifest

This file lists the skills installed in this memory fork. Generated at fork time by the onboarding orchestrator based on the peer's role.

---

## Bundle metadata

- **Role:** {{role}}
- **Fork created:** {{date}}
- **Source version:** {{source_version}}
- **Peer slug:** {{slug}}

---

## Installed skills

### Tier 1 — Universal
{{tier_1_skills}}

### Tier 2 — Engineering (if applicable)
{{tier_2_skills}}

### Tier 3 — Research & Intelligence (if applicable)
{{tier_3_skills}}

### Project-specific
{{project_skills}}

---

## How sync updates work

When the upstream `brainframe-memory-starter` publishes a new version of a skill in your bundle, you'll get a PR titled `[Skill Sync] v{version}`. You can:

- **Merge** — accept the update
- **Close** — skip this version
- **Partial** — cherry-pick individual skill updates

Skills NOT in your bundle are not pushed to your fork. You won't see PRs for skills outside your role tier.

---

## How to add a skill not in your bundle

Ask Dave. A skill update to your bundle requires an orchestrator re-run with expanded components.

## How to remove a skill

Delete the file from `SYSTEM/skills/`. Update this BUNDLE.md manifest to match. Sync PRs for that skill will still arrive (harmless) unless the upstream retires it.
