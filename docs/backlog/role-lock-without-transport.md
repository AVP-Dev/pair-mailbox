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
