# SYSTEM/skills/

Skills installed in this memory fork, selected based on the peer's role at fork time.

## Files

- `BUNDLE.md` — manifest listing installed skills, role, versions
- Individual skill files (named per skill, e.g. `quitchat.md`, `systematic-debugging.md`)

## How skills are organized

Skills are classified into four tiers (per `brainframe-comms/specs/admin/admin-skill-bundles-per-role-v1.md`):

1. **Tier 1 — Universal** — session discipline, writing, spec authoring. Any peer with a memory fork gets these.
2. **Tier 2 — Engineering** — code, debugging, TDD, security. Peers in engineering roles.
3. **Tier 3 — Research & Intelligence** — critique, research prompts, video intel. Premium value for clients; optional for employees.
4. **Tier 4 — Ops-Internal** — Dave's personal workflow. **Never shipped to peers.**

Your bundle is defined by your role. To see which tier each of your skills is in, check `BUNDLE.md`.

## Updates

New versions of skills in your bundle arrive as template-sync PRs. Skills outside your bundle never arrive (by design).

## Extending

If you need a skill not in your bundle, ask Dave. Orchestrator can re-run with expanded components; a PR will add the skill files and update `BUNDLE.md`.
