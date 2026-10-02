---
description: Wrap up the pairing — leave a durable handoff note and report what is still open
---

Wrap up this collaboration properly. Four steps, in order.

**1. Leave a durable note.** Anything the peer or your future self needs after a context reset
goes in a note, not the conversation:

!`bin/pairctl notes 2>/dev/null | tail -40 || true`

Call `pair_note` with kind `handoff`: what changed, what you deliberately did not do, and every
point you are unsure about.

**2. Close the review loop.** Read the peer's messages (`pair_read`) and its `finding` notes. For
each finding, open the file it cited and confirm with your own tools that the evidence exists and
says what was claimed. Fix what is real. Where a finding turns out to be wrong, say so in a
`verdict` note — that is as useful as confirming one, and it stops the peer repeating it.

**3. Release in-flight patches.** `pair_patch_release` for each exact path you reserved. Never
clean the patch directory with a glob: the peer may hold a file you cannot see.

**4. Report to the user.** Three parts, and do not blur them:

- **Done and verified** — changed, with the evidence you personally checked.
- **Proposed but not applied** — patches sealed and waiting on the writer, with their paths and hashes.
- **Still open** — findings you disagree with, anything unverified, anything you skipped on purpose.

A proposed patch is not a merged patch. A plan is not a result. If it is unverified, say it is
unverified.