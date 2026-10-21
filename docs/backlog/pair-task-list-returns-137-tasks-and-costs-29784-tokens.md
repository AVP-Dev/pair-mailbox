---
title: pair_task list returns every task ever opened — 29 784 tokens per call to say that one is open
found: 2026-10-21
severity: medium
round: 134
status: open
---

## Where

- `mcp/pair_mailbox/server.py:2616` — the `list` branch of `tool_pair_task`
- `mcp/pair_mailbox/server.py:2618` — the payload it returns: every item, unfiltered
- the live room: 137 tasks, 1 open, measured 2026-10-21

## What it costs

Measured on the live room with `tiktoken` `cl100k_base`, on the payload the tool
actually returns:

```
pair_task list   77 906 chars   29 784 tokens
```

That is 217 tokens per task, and the room opens roughly one task per round, so the
call crosses 30 000 tokens at round ~138 and reaches ~65 000 by round 300.

The call returns the whole history — closed, dropped and open alike — with full
metadata, so an agent that only needs "what is open right now" pays for 136 closed
tasks to learn that one is open.

## Why the estimate that started this round was wrong

The round's first estimate was ~12 000 tokens, from dividing the character count by
four. At 2.6 characters per token in Russian, that undercounts by 2.4×. Every
character-based estimate in this area needs a tokenizer before it is quoted.

## What to do

The default answer becomes the open window:

- `action="list"` returns the open tasks, plus counts (`open`, `done`, `dropped`) and
  `next_id`;
- the history is behind an explicit scope (`scope="history"`, with paging), so a
  caller that genuinely needs the closed tasks asks for them and says so;
- the tool description and `pair_digest` name the default, so an agent is not left
  guessing why the list is short.

Measured effect on this room: **29 784 → ~150 tokens per call**, and the routine
orientation (digest + state + task list + note index) goes from 38 292 to 893 tokens.

## Red-proof

- with the change, `pair_task(action="list")` returns every open task and no closed
  one, and the counts sum to the room's real totals;
- `pair_task(action="list", scope="history")` returns the same 137 items as today, with
  the same ids — no duplication, no loss (the failure mode the `state.json` rotation
  card measured: 222 union rows for 131 real records);
- a test that builds a room with 1 000 closed tasks and asserts the default answer
  stays under a token bound — the property that is being bought.

## Severity

medium — nothing breaks, and the cost is paid in context on every call that uses the
list. It is the single largest token sink in the room.
