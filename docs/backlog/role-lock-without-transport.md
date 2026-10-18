---
title: A held role lock no longer implies a server is serving
found: 2026-10-18T01:25Z
severity: medium
round: 106
status: open
---

`PairSession.acquire_role_lock` (mcp/pair_mailbox/server.py:914-923) refuses a
second process on a held role by `fcntl.flock(LOCK_EX|LOCK_NB)` alone. The kernel
keeps that lock for a live process, so a process whose client is gone still
answers "held".

Round 104 recorded the first form of this. Round 106 slice D made it sharper: a
socket daemon can now outlive its agent **by design**, so "lock held" no longer
even implies a CLI exists — the truthful statement is "some process holds the
role", which may be a live daemon with no agent, a corpse, or a healthy pair.

Why it is parked: the cure needs a liveness signal on the lock holder — did it
complete `initialize`, is a client attached — which changes restart semantics
for every role and touches the transport slice's own invariants (daemon outliving
its pane is a FEATURE there). Mixing the two would break one to fix the other.

Shape of the fix, for that round:

- on refusal, probe the holder: its pid, whether it holds a live transport, and
  report that in the message — "holder pid N has no client attached" instead of a
  bare "already held";
- a holder that lost its transport releases the lock itself (self-check on read
  failure), so the next raiser is not blocked by a corpse;
- the operator path stays `pairrestart --role <role>` (after round 106 slice A and
  S0 it now sees only this project's holders).

Evidence: round 104's measured corpse (pid holding a lock with no client), and
slice D's own test asserting that kill -9 releases the lock — which is the death
path, not the client-gone path.


## Measured update (12:33Z): the mechanics are CLOSED; only the socket-lane reaper remains

Re-measured on a scratch state dir (round 112's session, live pair untouched):

| case | observed | evidence |
|---|---|---|
| live holder | `role 'orphanrole' is held by a LIVE process (pid N)` | live pair: both roles; scratch daemon pid 1524731 |
| holder killed | kernel releases the flock -> `role 'orphanrole' is free` | kill on scratch, then describe() -> free, `flock_free` -> True |
| dead pid record | `FREE at the kernel level, but a pid record names dead pid N — reap it`, state `stale`, then `reap()` -> `['orphanrole']` and describe -> `free` | scratch with pid 999999999 |

So the corpse cases are handled by the kernel + lock_reaper, and the diagnosis
names each of them (round 111 slice 1). What the card's reaper/heartbeat asks for
is narrower than it read in round 108: a LIVE process whose PANE died. That case
CANNOT arise on the child lane (the daemon is the CLI's child — the pane's death
kills it), so it is a SOCKET-LANE hazard only, which means it is gated on the
live flip (docs/backlog/socket-flip-live-pair.md), not an independent task.

Dependency recorded rather than parked twice: heartbeat/reaper lands in the same
round as the socket flip, with its own red-proof (daemon alive, its pane gone,
the role must not read live forever).
