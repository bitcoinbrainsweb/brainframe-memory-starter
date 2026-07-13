---
name: pickup
description: >-
  Resumes a handoff session. Use when the operator says "pickup {slug}", "continue
  {slug}", or "resume {slug}". Finds the session row by pickup_slug, surfaces its
  context_brief, and executes immediately. No preamble, no re-asking.
---

# Pickup: resume from a handoff

Trigger: `pickup` plus any words. Fuzzy match always.

This is the database counterpart to handchat. Where handchat wrote a `context_brief`
and `pickup_slug` onto the current sessions row, pickup reads them back.

## Step 1: find the handoff row

Match the slug words against unclaimed handoffs. Join the words with dashes and look
for the row whose pickup_slug contains them:

```sql
SELECT id, project, pickup_slug, context_brief, handoff_chain_id, started_at
  FROM sessions
 WHERE pickup_slug IS NOT NULL
   AND pickup_slug ILIKE '%' || '{slug_words_joined}' || '%'
 ORDER BY started_at DESC
 LIMIT 5;
```

If multiple rows match, list them and ask which. If one matches, proceed. If none
match, list the most recent handoffs and ask the operator to pick. Never just say
"not found."

## Step 2: start the continuation session

Insert a new session whose `handoff_chain_id` is inherited from the matched row (so
the chain is preserved), then surface the brief:

```sql
INSERT INTO sessions (project, status, handoff_chain_id)
VALUES ('{project}', 'active', '{matched_handoff_chain_id}')
RETURNING id;
```

Keep the new session id in conversation memory as `current_session_id`.

## Step 3: surface and execute

Print the "Pick up here" line and the mental-model section from `context_brief`, then
immediately execute the next action. No confirmation, no "shall I proceed."

## Rules to preserve

1. Fuzzy match always: "pickup api rate thing" should work.
2. A new session row is created and inherits `handoff_chain_id` from the handoff, so
   the whole thread stays a single traceable chain.
3. Execute immediately after surfacing context.
4. If no match, list recent handoffs rather than failing.
