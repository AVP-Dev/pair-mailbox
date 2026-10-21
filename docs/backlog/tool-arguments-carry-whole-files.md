---
title: Tool arguments carry whole files — 232 065 tokens in one session, and the harness stores each call twice
found: 2026-10-21
severity: medium
round: 134
status: open
---

## Where

- the harness's session log: `~/.omp/agent/sessions/<project>/*.jsonl`
- `skills/pair-work/SKILL.md` — the guidance that shapes how an agent calls its tools
- measured 2026-10-21 on this project's longest session (17 498 782 B, 6 028 rows)

## What the measurement says

The session holds 3 046 227 tokens. Broken down:

| bucket | tokens | share |
|---|---:|---:|
| assistant tool calls | 915 434 | 30.1 % |
| tool results | 749 049 | 24.6 % |
| assistant thinking | 738 964 | 24.3 % |
| `custom` rows (`tool_execution_start`) | 330 999 | 10.9 % |
| compaction summaries | 263 161 | 8.6 % |

The call *arguments*, measured apart from the envelope, are 394 513 tokens across
1 988 calls: `write` 140 387, `bash` 129 817, `edit` 91 678. The six largest single
calls were 7 321, 5 291, 3 410, 2 963, 2 889 and 2 824 tokens — whole files carried
as arguments. `write` + `edit` arguments alone are 232 065 tokens of one session's
context, and they stay in it because nothing compacts them away any faster than the
rest.

The `custom` rows are the same call payload stored a second time: the harness writes a
`tool_execution_start` event carrying the call, while the message part already
carries it. In this session that duplication is 330 999 tokens (10.9 %); in a live
one, 6 557 of 53 677 (12.1 %).

Note what this is *not*: `bash` output averages 323 tokens per call. Command output is
not the sink. The shape of the calls is.

## What to do

Two halves, one in this repository and one in the harness:

1. **Guidance** — the pair skills should say what the tools already allow: read a file
   by range, never hand a whole file back as a call argument. A `read` with a range
   costs 5 953 tokens across 212 calls in the measured session; a `write` of a whole
   file costs thousands per call.
2. **Harness** — the `tool_execution_start` duplication is ~11 % of the context for no
   gain; storing the call once, or storing an event id, is worth more than any
   compression scheme.

## Red-proof

- an agent session that edits a 3 000-line file shows call arguments dominated by the
  *changed region*, not the file;
- the session log holds one copy of each call payload — a test that counts the
  occurrences of a known payload string in the log sees one, not two.

## What round 135 did, and what is left

**Done (the repository's half).** The pair skills now say what the tools already
allowed: read a file by range, never hand a whole file back as a call argument
(`skills/pair-work/SKILL.md` §7c and its Russian mirror). The guidance is in place;
what it buys depends on the agents reading it.

**Left (the harness's half).** The `tool_execution_start` duplication — 330 999
tokens in the measured session, 10,9 % of the context — lives in the harness's own
session store, not in this repository. Storing the call once, or storing an event id
instead of the payload, is a change in the harness. It stays here until that
project takes it, and the red-proof above still names the property to check.

## Severity

medium — the cost is invisible from the tool side: every tool did exactly what it was
asked, and the context paid for the asking.
