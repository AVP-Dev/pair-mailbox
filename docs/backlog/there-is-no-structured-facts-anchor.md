---
title: There is no structured facts anchor — a compaction keeps 39 513 to 49 131 tokens of prose alive
found: 2026-10-21
severity: medium
round: 134
status: open
---

## Where

- the harness's own compaction, method `snapcompact` — measured in
  `~/.omp/agent/sessions/<project>/*.jsonl`
- `mcp/pair_mailbox/digest.py` — the pair's own orientation slice, capped at 500 tokens
  (`DIGEST_TOKEN_BUDGET`, `digest.py:40`)
- `mcp/pair_mailbox/server.py:3351` — `tool_pair_digest`

## What the measurement says

Seven compactions in one long session, with the token counts the harness records:

| before | after |
|---:|---:|
| 462 297 | 45 036 |
| 790 583 | 49 131 |
| 363 817 | 46 783 |
| 188 331 | 44 712 |
| 109 070 | 41 850 |
| 139 729 | 39 513 |
| 125 796 | 41 826 |

Compaction removes ~90 % of the context and works. The problem is the floor it
leaves: **39 513–49 131 tokens**, of which the compaction row itself is 34 831–39 814
tokens. That is the "key context" the round's task asks about — it survives every
later turn — but it is prose, written once, carried forever, and it cannot be queried,
diffed, or dropped.

The pair side has no structured equivalent. The notes (`plan`, `decision`, `finding`,
`verdict`) are the closest thing to a fact store, and they are append-only prose
chunks: a tail read of the four working notes costs 17 052 tokens.

## What to do

A facts surface with a hard budget:

- a small key → value store (the round's invariants: the task, the current phase, the
  branch, the head commit, the open blocker, the decision in force) held in 200–500
  tokens;
- carried by `pair_digest`, which is already built to be the cheap orientation
  (`digest.py:40`);
- written by the writer at the points where an invariant actually changes, and
  replacing prose in the notes that the facts already hold.

The questions this needs answered before code, which is why it is a card and not a
patch:

1. where the facts live — `state.json` (rotates, and the `state.json` growth card is
   open) or a separate `.pair/facts.json` (its own lifecycle, its own rotation);
2. who writes them — the writer only, or any agent that changes an invariant;
3. what happens at round close — facts cleared, or kept as the next round's starting
   point;
4. whether the harness's compaction summary can be *replaced* by the facts, or only
   supplemented. Only the first makes the ~39 000-token saving real; the second
   merely adds 500 tokens to a 40 000-token carry.

## Red-proof

- after a compaction the facts surface answers "what is the task, the phase, the
  branch, the head, the blocker" in under 500 tokens, with no history read;
- the facts never exceed their budget: a fact write that would cross it is refused,
  not truncated silently;
- a test asserts the facts survive a context reset — the property the prose anchor
  gives only at the price of 39 000 tokens.

## Severity

medium — it is the difference between an agent re-reading 30 000–80 000 tokens of
history after every reset and reading 500.
