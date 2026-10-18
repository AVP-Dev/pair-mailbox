---
title: Apply the socket flip to the live pair — operator procedure, human hands
found: 2026-10-18T15:10Z
severity: medium
round: 113
status: open
---

The code is proven and stays in the tree: the thin client (round 107), the daemon
raise in pairup (round 106 slice G), the persisted transport mode and bounded
re-dial (rounds 112–113). Real-process tests for all of it.

What is NOT proven, and broke a working system when an agent applied it anyway:
flipping a **live** pair. Sequence of the mistake, recorded so it is not
repeated: both entries switched while both CLIs ran; the env-vs-state defect was
found only afterwards; the peer's session sat `pair Failed` for the rest of the
session; and the agent that made the change could not perform the step that
finishes it — the peer's CLI restart.

## The boundary rule

A change to a shared surface whose finishing step is **another agent's restart or
another session** is an operator procedure, not an autonomous action. The tell
was visible at the time: the procedure note said "the restart is the human's" and
the actions crossed the line anyway. If that sentence is in your plan, the plan
stops at the file write and hands the rest over.

## Procedure (docs/socket-transport-flip.md)

1. `pairctl status` — confirm the lane and the attach line.
2. Persist the mode (installer writes `transport: socket` into state).
3. Raise the daemon for the role (`PAIR_TRANSPORT=socket pairup`, or by hand — a
   live daemon holds the role lock, so raise it BEFORE the swap).
4. Swap the entry (installer rewrites it; the child-server line stays as a comment
   for one round).
5. **Restart the CLI — this step is the human's.** Until it happens the running
   session keeps the child lane; that is correct, not a failure.
6. Verify: `pgrep` for the daemon by fd-scan on `.server-<role>.lock`, never by
   argv shape (three false DOWN verdicts measured); the pane shows pair Connected.

## Rollback, measured

Kill the daemon (the role must read FREE — otherwise the restart wedges on the
orphan), reinstall with PAIR_TRANSPORT unset, set state `transport: child`.
Verified live: the peer's child-server took its role back with its CLI as parent.

## Out of scope for this card

A daemon owned by no CLI across a **reboot**, a supervisor that raises daemons
for both roles at session start, and `pairrelaunch` switching lanes — all belong
to the round that owns daemon lifecycle, not to the flip.
