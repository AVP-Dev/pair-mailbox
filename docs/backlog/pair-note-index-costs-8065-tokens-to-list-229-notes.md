---
title: pair_note_index spends 8 065 tokens to list 229 notes' metadata
found: 2026-10-21
severity: low
round: 134
status: open
---

## Where

- `mcp/pair_mailbox/server.py:3149` — `tool_pair_note_index`
- `mcp/pair_mailbox/server.py:3163` — the five fields it returns per note
- the live room: 229 notes, 3.4 MB, measured 2026-10-21

## What it costs

```
pair_note_index   21 635 chars   8 065 tokens
```

The call reads no content — it is metadata by design, and the docstring says so. But
229 rows of five fields (kind, lines, entries, bytes, archived) is 35 tokens per note,
and the answer is 20× more expensive than `pair_digest`, which answers the question
the index is usually called for. The rows are useful; all 229 of them at once are not.

The guidance compounds it: `skills/pair-work/SKILL.md:254` tells the agent to call
`pair_note_index` *before* reading notes, so the cost is on the recommended path.

## What to do

- return the N freshest notes (the ones a reader can act on) plus the totals:
  `count`, `total_bytes`, and the biggest note by size;
- page the rest behind an explicit argument, the way variant A pages the task history;
- keep the per-note fields in the paged answer — the sizes are what let an agent
  decide which note is worth reading.

Measured effect: **8 065 → ~300 tokens per call** for the default answer.

## Red-proof

- the default answer lists the freshest notes and reports the room's true totals;
- a note outside the default window is still reachable, and the answer says how many
  were left out — the same property the `truncated` field gives `pair_note_read`;
- a test with 1 000 notes asserts the default answer stays under a token bound.

## Severity

low — the call is useful and correct; it is only expensive, and only on the path the
skill recommends.
