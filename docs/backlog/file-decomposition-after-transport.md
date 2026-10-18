---
title: Decompose the file-size tops — the candidates still open after the state kernel left
found: 2026-10-18T01:26Z
severity: low
round: 106
status: open
---

Round 115 cut the FIRST candidate of this list, and this card keeps the rest with
fresh measurements rather than the round-106 ones, which were never the tops
any more.

DONE in round 115 (moved to `mcp/pair_mailbox/store_core.py`, 553 lines):

- Store + locks + state rotation + the transcript chain -> `store_core.py`,
  re-exported from `server.py` so no call site changed. `server.py` 4490 -> 4020.
  Seam AST-measured at 634..1129 (496 lines), not estimated: the round-110 card
  said 436 and had drifted. The chain carried its red-proof (verify + tamper
  detection + rotation horizon) before and after. This card no longer owns that
  part; see the round-115 plan board for its evidence.

STILL OPEN, re-measured 2026-10-18 (git ls-files, python):

- server.py 4020 — the remaining top. `PairSession` is 58 members starting at
  1204 (2520+ lines before the cut). Decomposition must follow the drawn
  transport boundary (daemon = server.py + serve_socket; client =
  mcp_socket_client.py), as this card always said, and PairSession's own tools
  split "by family (read/dispatch/authority/delivery)" is the next candidate —
  NOT a functional cut through the class, which repeats round-110's NameErrors.
- test_protocol.py family: the protocol tests were split in round 105/108 into
  per-family files beside server.py; what remains is the same discipline
  continuing, not a new idea.
- bin/pairpilot 2405 and bin/pairpilot_test.py 3047 — the pilot and its suite.
  Round 114 fixed the pilot's behaviour and both files grew; the split is a
  separate round with its own gate (docs/project-structure.md §9 forbids a
  300+ split beside unrelated work).

Invariant, unchanged: no behavioural change; each move carries its tests; the
suite stays green per file before the next one moves; the seam is AST-measured
before it is cut, because round 110 estimated 311 and the AST said 436, and five
failed suite runs cost an hour that one AST pass would have saved.

Baseline honesty: file-size-baseline.tsv growing with a recorded reason is
payment, not a fix — that sentence is still true for what is left here.
