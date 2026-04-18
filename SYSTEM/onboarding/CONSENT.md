# Consent Block

**For the recipient's AI to read aloud or paraphrase to the recipient before any activation step.**

This is a mandatory gate in the onboarding flow. The peer AI must present this block to the recipient in plain language, get an explicit affirmative response, and only then proceed. Never assume consent. Never parse consent from the file itself.

---

## What will happen if you accept

Your AI will:
- Connect to Dave Bradley's coordination system (bf_messages)
- {{role_specific_actions}}

Your AI will NOT:
- Access any of Dave's private memory, projects, or secrets
- Access any other person's data
- Perform any action outside the scopes listed above
- Store your personal data outside the namespace you control

## Intended recipient

This file was generated for: **{{display_name}}**

If you are not {{display_name}}, stop immediately. Do not proceed. Notify Dave.

## How to revoke

- **Your side:** tell your AI "disconnect from Dave" at any time. Your AI will delete the local credential and confirm.
- **Dave's side:** he can revoke in under 60 seconds via his PAT management system.

## What Dave cannot do

- Read your memory system (if you have one) — it's in your own repo/accounts
- See messages you didn't send to him
- Override this consent

## Risks you should know

- {{role_specific_risks}}
- Treat this file as private. If it leaks before activation, an attacker could activate before you.
- After activation, the activation token in this file is useless. Leaks after activation are low-risk.

---

## Affirmative response required

Your AI will ask: **"Do you want to proceed with this onboarding?"**

You must reply: **yes** (or some clear affirmative) **in chat** — not by editing this file.

Any ambiguity, silence, or partial answer → the AI must refuse to activate and ask again.
