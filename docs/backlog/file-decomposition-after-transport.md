---
title: Decompose the file-size tops after the transport boundary is drawn
found: 2026-10-18T01:26Z
severity: low
round: 106
status: open
---

Measured tops (git ls-files, round 106): test_protocol.py 4643, server.py 4309,
pairpilot_test.py 2749, pairpilot 2078, composer.py 1465. file-size-baseline.tsv
grows every round with a recorded reason, which is payment, not a fix.

Why deferred until now rather than done ad hoc: round 106's socket transport
draws the process boundary (daemon = server.py + serve_socket; client =
mcp_socket_client.py). An honest split should FOLLOW that boundary; splitting
earlier would have meant split-then-re-split.

Candidate split, each module with its own test file beside it, as the repo already
does for bin/*:

- Store + locks + state rotation -> its own module (state_rotation.py is the seed
  of the last part and already lives apart)
- MCPServer transport (stdio + socket) -> its own module
- PairSession tools, by family (read/dispatch/authority/delivery)
- gates: done_blockers, plan_gate, review — the close path currently spans three
  modules through lazy imports precisely to avoid cycles

Invariant for the split: no behavioural change; each move carries its tests, and
the suite stays green per file before the next one moves.
