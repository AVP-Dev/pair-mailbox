---
title: pair_note_read budgets lines, not tokens — a 120-line tail costs 3 166 to 5 122 tokens
found: 2026-10-21
severity: low
round: 134
status: open
---

## Where

- `mcp/pair_mailbox/server.py:3176` — `tool_pair_note_read`
- `mcp/pair_mailbox/server.py:158` — `NOTE_READ_DEFAULT = 120` (lines)
- `mcp/pair_mailbox/server.py:173` — `NOTE_READ_MAX = 400` (lines)
- the live room's four working notes, measured 2026-10-21

## What it costs

A tail read of 120 lines, measured on the live room:

| note | file | tail tokens |
|---|---:|---:|
| `decision` | 77 492 B | 5 122 |
| `finding` | 62 696 B | 4 391 |
| `handoff` | 76 260 B | 4 373 |
| `plan` | 63 958 B | 3 166 |

Reading the four working notes as the skill recommends costs **17 052 tokens**.

The budget is a *line* count, and a line of Russian prose is not a fixed number of
tokens: a note of long lines costs four times a note of short ones, and the reader
cannot tell in advance which one it opened. The call is described as returning
"approximately tail size" (`server.py:3736`) — approximately in lines.

## What to do

- budget the read in tokens, not lines: the default answers with at most N tokens of
  the tail, and reports the token count it returned;
- keep the existing cut discipline — the cut lands on a record edge, and a token
  budget must land on the same edge, never inside a record;
- keep `tail_lines` working for callers that want a line window, and say which budget
  applied.

Measured effect: a four-note sweep goes from **17 052 → ~3 200 tokens**.

## Red-proof

- the returned tail is under the token budget for a note of long lines and for a note
  of short lines — the property the line budget could not hold;
- the cut never splits a record: the answer's first line is a `## ` heading whenever
  anything was cut, exactly as today;
- a test with a note of 1 000 long lines asserts the token bound and the intact-record
  property together.

## Severity

low — the call is honest about its cut; it is the unit of the budget that is wrong.
