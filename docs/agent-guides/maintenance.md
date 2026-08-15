# Maintenance Guide

This file is the single home for maintenance cadence. `SYSTEM/GLOBAL_RULES.md` names the triggers and points here.

## Event-driven checks (run these regardless of the calendar)

Two events invalidate a passing check, and neither is on a schedule:

1. **A major model release.** Re-run the full harness audit: the quickstart demos (`harness/quickstart/demo_*.py`), the foreman gates, and a read-through of every `SKILL.md` for instructions that assumed the old model's behaviour. A prompt tuned to one model generation is not evidence about the next one.
2. **A novel failure that recurs.** The first occurrence is noise. The second is a pattern: stop, re-run the harness audit, and either fix the cause or write the failure mode into the relevant skill's Failure modes table so the next session degrades cleanly instead of rediscovering it.

Record what the audit found. An audit that produced no written output did not happen.

## Monthly checks

1. **Credential validity**: Run `source ~/.config/memory-starter/.env && curl -s "$SUPABASE_URL/rest/v1/topics?limit=1" -H "apikey: $SUPABASE_ANON_KEY" | head -1`. If you get a 401 or empty response, your Supabase anon key may have been rotated. Get the new key from Supabase dashboard > API Settings.

2. **Supabase connectivity**: Open your Supabase project dashboard. If the project is paused (free tier pauses after 7 days of inactivity), unpause it. The smoke test in onboard.sh will also catch this.

## Quarterly checks

1. **Claude Project instructions vs CLAUDE.md**: Compare the instructions you have pasted into your Claude Project with the current `CLAUDE.md` in this repo. If they differ, the repo version wins. Update your Project instructions. Check for the version line at the top.

2. **Skill trust sections**: Review each skill in `.claude/skills/`. If you have added new external calls or write paths, update the Trust section.

3. **USER/ file drift**: Re-read `USER/routing/facts.md` and `USER/routing/preferences.md`. Update anything stale.

## First debugging step for common failures

**"Claude doesn't seem to remember anything"**: Check that your Claude Project instructions are still there (Claude occasionally loses them on plan changes). Re-paste from `SYSTEM/PROJECT_INSTRUCTIONS_OWNER.md`.

**"Supabase queries return 401"**: Run `source ~/.config/memory-starter/.env && echo $SUPABASE_ANON_KEY`. If empty, rerun `bash scripts/onboard.sh`. If present, check your Supabase project is unpaused.

**"Skill not activating"**: Type the skill name explicitly (e.g. "run quitchat"). If it still fails, re-read the SKILL.md for that skill and check the Trust section has no broken paths.

## Credential rotation

When a Supabase anon key is rotated:
1. Get new key from Supabase dashboard > API Settings > anon public
2. Edit `~/.config/memory-starter/.env` with new value
3. Run smoke test: `source ~/.config/memory-starter/.env && curl -s "$SUPABASE_URL/rest/v1/topics?limit=1" -H "apikey: $SUPABASE_ANON_KEY"`
4. If 200: done. If not: check project is unpaused and key is correct.
