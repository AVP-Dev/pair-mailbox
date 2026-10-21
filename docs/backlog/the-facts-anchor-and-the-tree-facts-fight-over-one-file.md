---
title: The facts anchor and the tree-facts collector fight over one file — the anchor is empty after every round close, and an anchor write breaks the ground-truth gate
found: 2026-10-21
severity: medium
round: 136
status: open
---

## Where

- `mcp/pair_mailbox/facts.py:27` — `FACTS_FILE = "facts.json"`; `write_facts`
  (`facts.py:73`) writes `{"facts": [...], "updated_at": ...}` to
  `.pair/facts.json`
- `mcp/pair_mailbox/session_facts.py:21` — `FACTS_FILE = "facts.json"`;
  `save_facts` (`session_facts.py:327`) writes
  `{"version": 1, "head", "dirty_hash", "touched", "untracked", "mock_flags",
  "test_runs", ...}` to the same path
- `mcp/pair_mailbox/server.py` — the `pair_facts` MCP tool, the anchor's only
  writer
- `scripts/verify-ground-truth.py` — the gate that reads the collector's file
  (`session_facts.load_facts`)

## What breaks

Two features of round 135 were given the same filename, and each write replaces
the other's content.

**The anchor is empty after every round close.** `facts.read_facts`
(`facts.py:41`) looks for a `facts` list; the collector's file has no such key,
so the anchor reads as **no facts** — not an error, just an empty anchor. And
the close ritual runs the collector: the round-135 evidence itself says
`session_facts + anti-mock + round-summary + verify-ground-truth`. So the
anchor that was built to survive a reset is wiped by the very ritual that
closes the round, and the next session starts with an empty anchor. Measured
on this room, 2026-10-21: `pair_facts list` on a room whose `.pair/facts.json`
was written by the collector → `{"facts": [], "count": 0}`.

**An anchor write breaks the ground-truth gate.** `facts.write_facts`
replaces the whole file with `{"facts": [...]}`. `session_facts.load_facts`
(`session_facts.py:335`) then reads a file whose `version` is missing:
`FactsError` — *"a foreign-version facts.json, or not a dict"*. The
ground-truth gate turns that into `BLOCKED - input never reads` (exit 2).
A writer who records a fact before the close ritual therefore breaks the
check that verifies the close.

Neither side complains: the anchor degrades to "no facts" and the collector
degrades to "no version". Both are the fail-quiet direction.

## What to do

- Give the two files two names. The collector's file describes the TREE (head,
  dirty hash, touched files, test runs) and the anchor's file describes the
  ROUND (key → value). They are different things and they should not share a
  path: e.g. `.pair/tree-facts.json` for the collector, `.pair/facts.json` for
  the anchor — or the other way round, as long as the names differ.
- Then fix the references: `scripts/verify-ground-truth.py` takes the facts
  path as an argument (the close ritual passes it), `mcp/pair_mailbox/
  session_facts.py`'s `FACTS_FILE`, the anti-mock gate's input, and the
  session-finalizer's script that writes the collector's file.
- Read the anchor's file with the anchor's reader and the collector's file with
  the collector's reader. A file that means two things is a file whose meaning
  depends on who wrote it last.

## Red-proof

- a room where a fact was set through `pair_facts`, then the collector runs:
  `pair_facts list` still shows the fact, and `scripts/verify-ground-truth.py`
  still passes;
- the reverse order: the collector runs, then a fact is set — the collector's
  facts are still readable and the ground-truth gate still passes;
- the two files are separate paths, and neither writer can blank the other.

## Severity

medium — the round-135 deliverable ("a structured anchor that survives a
reset") does not survive a reset in practice, and the failure is silent in
both directions: an empty anchor reads as "no facts yet", and a blocked
ground-truth gate reads as a broken gate rather than as two writers on one
path.
