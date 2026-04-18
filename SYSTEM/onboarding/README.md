# SYSTEM/onboarding/

Peer-side onboarding scaffolding. Files in this folder are read by the peer AI when a fresh deliverable is pasted into their chat environment.

## Files

| File | Purpose | When read |
|---|---|---|
| `INTEGRITY.md` | Supply-chain hash verification | First — before anything else |
| `CAPABILITY_CHECK.md` | Verify peer AI can do what the role needs | After integrity passes |
| `CONSENT.md` | Plain-English consent block for recipient | After capability passes |

## Order of operations

1. Peer pastes deliverable file into their AI
2. AI reads INTEGRITY.md → verifies file hash against brainframe-public
3. AI reads CAPABILITY_CHECK.md → runs self-check, reports to recipient
4. AI reads CONSENT.md → presents to recipient, gets affirmative
5. AI proceeds with role-specific activation (PAT exchange, memory fork setup, etc.)

## Who maintains this folder

Upstream (`brainframe-memory-starter`). Updates propagate to peer forks via template-sync PRs.

## What happens if these files are missing

The deliverable generation process should fail closed. An orchestrator-generated file that references missing onboarding files should not be sent to a peer. This is a maintainer invariant, not a runtime check.
