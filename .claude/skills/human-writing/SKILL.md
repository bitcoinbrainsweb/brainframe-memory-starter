---
name: human-writing
description: >-
  Governs polished human-facing prose for external or interpersonal readers.
  Also handles "write as [author]" and "in [author]'s voice" requests using
  USER/voice/ author files. Use for: emails, social posts, announcements, UI copy,
  client messages, anything a real human reads. Triggered by: write an email,
  draft a post, compose a message, write copy, make this sound human, polish this
  for publication, write as [name], in [name]'s voice.
version: 1.1.0
---

# Human Writing

For prose that a real human will read. Not for specs, docs, or internal notes.

---

## When to use

- "write an email"
- "draft a post"
- "compose a message"
- "write copy for X"
- "make this sound human"
- "polish this for publication"
- "write as [name]"
- "in [name]'s voice"

Do NOT use for:
- Specs (use spec-writing)
- Technical docs or READMEs
- Internal session notes

---

## Voice cloning mode

When the request includes "write as [name]" or "in [name]'s voice":

1. Fetch `USER/voice/_floor.md` from repo -- universal bans, always applies
2. Fetch `USER/voice/authors/[slug].md` -- signature traits for this author
3. If author file not found: list available authors from `USER/voice/authors/` and ask Dave to pick
4. Draft using both files
5. Run voice check from the author file before delivering

The floor always wins over the author file. Never reproduce a floor-banned pattern even if an author would use it.

---

## The strip test

Before writing, apply this test to every sentence:

Could this sentence have been written by a competent human who cares about the topic?

If no: rewrite it. Common failures:
- "I hope this finds you well" -- cut it
- "In today's fast-paced world" -- cut it
- "It's important to note that" -- cut it
- "As an AI language model" -- never
- Passive voice when active works -- fix it
- Adjective stacking ("innovative, cutting-edge, transformative") -- pick one or none

---

## Voice principles

**Direct.** Say the thing. Subject, verb, object. Then stop.

**Specific.** "We shipped the new dashboard on Tuesday" beats "We've been making progress."

**Earned confidence.** State positions without hedging them to death. If uncertain, say so once cleanly -- not with three qualifiers.

**Appropriate register.** Match the audience:
- Professional email: formal but warm, no slang
- Social post: punchy, opinionated, short
- Client message: clear, no jargon, outcome-focused
- UI copy: scannable, action-oriented, never cute at the expense of clear

---

## Process

1. Identify mode: standard prose or voice cloning?
2. For voice cloning: fetch floor + author file first (see Voice cloning mode above)
3. Draft once, without self-censoring
4. Apply the strip test. Cut anything that fails.
5. Read aloud mentally. If it sounds like a press release, cut more.
6. Deliver. Do not offer multiple versions unless asked.

---

## Format rules

- No em-dashes -- use commas, semicolons, or periods instead
- No bullet lists in prose contexts (emails, posts, messages) unless structure genuinely helps
- Short paragraphs: 2-3 sentences max in emails and posts
- Subject lines (emails): specific and scannable, not clever

---

## Rules

1. Deliver one version, polished. Offer alternatives only if asked.
2. Never explain what you did -- just do it.
3. If the user's draft is better than yours would be, say so and suggest light edits only.
4. AI slop is the primary failure mode. Read the output as a skeptical human before delivering.
