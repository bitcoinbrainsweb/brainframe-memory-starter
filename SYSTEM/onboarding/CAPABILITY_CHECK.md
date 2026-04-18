# Capability Check

**For the peer AI to run before presenting the consent block.**

The peer AI must verify it has the capabilities needed for its role. Fail loud, never silently degrade.

---

## Self-check — peer AI completes and reports

```
[ ] C1: Can you see this entire file? (yes/no)
[ ] C2: Can you persist a secret across sessions? (Claude Projects file / custom instructions / Cursor storage)
[ ] C3: Can you make HTTPS calls? (required for activation + messaging)
[ ] C4: Can you execute Python snippets? (required for bf_messages)
[ ] C5: Will you remember this instruction next session? (required for memory system)
```

## Decision tree

### If C1 is no
Stop. File isn't fully visible. Ask the recipient to paste the full file.

### If C3 or C4 is no
**Refuse activation.** No fallback for v1. Tell the recipient: "Your AI environment doesn't support the HTTPS or Python capabilities needed. Contact Dave for a manual alternative."

### If C2 or C5 is no (but C3 and C4 are yes)
- **For friend or client-messaging roles:** proceed, but warn: "This environment won't persist credentials between sessions. You'll need to re-paste the file each session."
- **For client-with-memory, subcontractor, or employee roles:** **Refuse activation.** These roles require persistent storage. Tell the recipient: "Use Claude Desktop with Projects, ChatGPT with Projects, or Cursor. This environment doesn't support persistent memory."

### If all checks pass
Proceed to CONSENT.md.

---

## Rules for the peer AI

1. Run this check BEFORE reading any activation code, BEFORE presenting the consent block.
2. Report results honestly. If you're unsure about C2 or C5, say "unknown" not "yes."
3. Do not guess. If you can't verify, treat as "no."
4. Do not proceed past a refusal. The recipient must contact Dave.
