# Voice System

Gives Claude the ability to write in specific authors' voices while holding all output to the universal slop floor.

## Structure

```
USER/voice/
  _floor.md           Universal rules -- applies to ALL authors, ALL surfaces
  README.md           This file
  authors/
    _template.md      Copy to create a new author file
    your-name.md      Your own voice (primary use case)
    [others].md       Collaborators, clients, anyone Claude writes as
```

## How to add an author

1. Copy `authors/_template.md` to `authors/[slug].md`
2. Fill in the frontmatter and voice traits
3. When asking Claude to write in that voice, say: "write as [name]" or "in [name]'s voice"

## How Claude uses these files

When triggered by "write as [name]" or "in [name]'s voice":
1. Fetch `USER/voice/_floor.md` -- universal bans, always applies
2. Fetch `USER/voice/authors/[slug].md` -- signature traits for this author
3. Draft using both
4. Run strip test from floor before delivering

## Tips

- The floor wins if it conflicts with an author file. Never add floor-banned patterns to an author file.
- Keep author files short. Claude needs signal, not biography.
- Your own voice file is the most valuable one. Write it by describing how you actually write, not how you wish you wrote.
