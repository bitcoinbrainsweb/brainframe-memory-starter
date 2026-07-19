---
name: design
description: >-
  Builds distinctive, production-grade frontend interfaces. Use when user says:
  build a component, build a page, build a site, build a UI, design a landing page,
  make this look good, style this, write the React for X, frontend for X.
  Produces HTML/CSS/JSX. Avoids generic AI aesthetics.
version: 1.0.0
---

# Design

Production-grade frontend. No generic AI aesthetics. No corporate clip-art vibes.

---

## When to use

- "build a component"
- "build a page / site / UI"
- "design a landing page"
- "make this look good"
- "style this"
- "write the React for X"
- "frontend for X"

Do NOT use for:
- Backend logic or data processing
- Editing an already-deployed site (use direct file edit approach)
- Pure content writing

---

## Design principles

**Personality over polish.** A design with a point of view beats a technically correct but forgettable one.

**Reference points worth stealing from:**
- The Pudding (data storytelling, editorial design)
- Stripe Press (typography, whitespace, restraint)
- Maggie Appleton (illustration + explanation combined)
- xkcd what-if (personality, specificity, delight in small moments)

**Things to avoid:**
- Hero image with gradient overlay and centered h1 -- done to death
- Card grids with drop shadows on everything
- "Get started" buttons in purple
- Roboto or Inter with default tracking everywhere
- Animations that exist to show off animations

---

## Typography floor (minimum viable)

- Font size: never below 16px body
- Line height: 1.5-1.65 for body, tighter for headings
- Measure: 60-75 chars per line for reading comfort
- Weight contrast: use at least two distinct weights (e.g. 400 body, 700 heading)
- Don't mix more than 2 typefaces

---

## Process

1. Ask (if not clear): What is this for? Who uses it? What must it do? What should it feel like?
2. State your design direction before writing code: 2-3 sentences on the aesthetic bet you're making.
3. Write the code. CSS variables for all colors and spacing -- never hardcoded hex in component logic.
4. Deliver a complete working file (HTML or JSX), not a skeleton.

---

## Output format

**For HTML:** Single file with `<style>` block inline. No external dependencies unless essential.

**For React/JSX:** Single component file. Tailwind for utility classes if available; otherwise inline style object or CSS module approach stated explicitly.

**Always include:**
- A clear component or page boundary
- Hover/focus states on interactive elements
- At least one moment of personality (a detail, a transition, a piece of copy that doesn't sound corporate)

---

## Quality gates before delivery

- [ ] Does this look like something a human with taste would be proud of?
- [ ] Would a designer call it "safe but good" or would they call it interesting?
- [ ] Are all interactive elements keyboard-accessible?
- [ ] Does it work without JavaScript for the core content?

---

## Rules

1. State the aesthetic direction before writing code.
2. Never ship a design without at least one personality detail.
3. CSS variables for all design tokens -- never hardcode.
4. If you catch yourself making something generic, stop and ask why.
5. Deliver complete, working code -- not scaffolds or TODOs.
