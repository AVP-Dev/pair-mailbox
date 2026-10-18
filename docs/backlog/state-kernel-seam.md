---
title: Cut the state kernel out of server.py — 436 lines, measured, needs its own round
found: 2026-10-18T10:25Z
severity: medium
round: 110
status: open
---

The largest file in the repo is `mcp/pair_mailbox/server.py` at 4478 lines with a
paid baseline reason (payment, not a fix). The honest seam is **measured**, not
estimated — that measurement is what this card carries.

## The boundary

State kernel: **lines 636..1071** (436 lines) — `ProtocolError` (636),
`_transcript_hash` (649), `verify_transcript_chain` (655), `_transcript_entry`
(702), `_transcript_tail` (719), `class Store` (761..1071), `atomic_write_json`
(1073), plus the constants `CHAIN_GENESIS` (634), `TRANSCRIPT_ARCHIVES`,
`TRANSCRIPT_MAX_BYTES` and the rotation merge `_state_rotation_load` (1113).

Everything else in server.py is protocol/gates/tools/transport (1..635 and
1072..4478).

## Why it was NOT cut in round 110

The peer's ruling (seq 1627): the transcript chain is the audit trail the whole
round-close gate depends on, and a move that can corrupt it *silently* — where
the suite may stay green — is not proportionate to file-size hygiene. Cutting
only `class Store` (311 lines) failed with five NameErrors because the class
reaches into the chain helpers: the seam is 436 wide, not 311. A wider seam in
load-bearing code is MORE risk, not less.

## Acceptance for the round that cuts it

- **Transcript-chain integrity as the red-proof, before and after the move**: the
  hash chain verifies, tamper detection still fires on the merged view, and the
  chain's anchors survive a rotation. A green suite alone is NOT the proof — the
  failure mode is silent corruption, which a suite does not see.
- The seam is re-measured with AST at cut time (line numbers drift with every
  commit; 636..1071 is a round-110 measurement).
- Callers keep working through re-export (`from store_core import Store,
  ProtocolError, ...`) so no call site changes in the same commit.
- The count invariant from round 108's test split applies: suites green before
  AND after, same count, same skips.
- After the cut, server.py should be ~4040 and the new module ~440; if both
  still need baseline reasons, say so honestly rather than declaring victory.

## Method note

Seam size is measured, not estimated. The round-110 attempt estimated 311 + "39
helpers" and the AST said 436. One AST pass answers it; five failed suite runs
cost an hour.
