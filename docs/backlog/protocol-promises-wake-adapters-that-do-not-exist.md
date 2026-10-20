---
title: PROTOCOL §7 promises wake adapters for agy, Claude and Codex — the code ships one (opencode) and the live transcript shows 31 failed wake-ups
found: 2026-10-19T23:28Z
severity: medium
round: 126
status: open
---

## Where

- `docs/PROTOCOL.md:400-403` — the adapter table (and `docs/PROTOCOL.ru.md`,
  the mirrored section)
- `bin/pairpilot:790` — `WAKE_ADAPTERS = {"opencode": "wake_opencode"}`
- `bin/pairpilot:817-833` — the dispatch and the `pilot_wake_no_adapter` row
- `mcp/pair_mailbox/wake_opencode.py` — the only adapter that exists

## What breaks

The protocol states, as fact, which CLI has which wake adapter:

| CLI | adapter | if unsupported |
|---|---|---|
| OpenCode | plugin + host API | reminder only, mailbox still delivered |
| Antigravity (`agy`) | native reactive messaging | reminder only |
| Claude, Codex | `tmux send-keys` | reminder only |
| anything else | none | reminder only |

The third column only makes sense if the second column is sometimes empty — it
says what happens to CLIs that have NO adapter. By that reading, agy, Claude
and Codex HAVE one.

The code has exactly one:

```python
# bin/pairpilot:790
WAKE_ADAPTERS = {"opencode": "wake_opencode"}
```

There is no agy adapter and no `tmux send-keys` wake path. `grep -n "send-keys"
bin/pairpilot` finds one hit — `bin/pairpilot:1595` — and that is not a wake: it
sends `Escape` to dismiss a stuck permission prompt.

The live transcript of this room measures the gap:

```
Counter({'omp': 17, 'agy': 14})   # pilot_wake_no_adapter, by target role
Counter()                          # pilot_wake_api_failed — zero
```

31 recorded failures to wake a role through an API, in 3757 transcript rows.
Zero API failures — the adapters that exist did not fail; the ones that do not
exist were never tried.

For the current pair this means: **agy is never woken through an API**. The
protocol's own framing (§7) is that "the mailbox is the guarantee, delivery is
the accelerator" — so this is not a broken round, but the operator reading
§7 believes there is a wake path for agy that does not exist, and the pilot
rows say so 14 times without anybody reading them.

## What to do

Either implement the promised adapters, or correct the table.

Implementing is the larger job: agy's "native reactive messaging" needs the
agy's own surface, and `tmux send-keys` for Claude/Codex has the same
composer-visibility problem the protocol already discusses for
`pair_deliver` (`docs/PROTOCOL.md:388`) — it must be behind the same two
gates the pilot's nudge passes (`docs/PROTOCOL.md:411`, gates at `:424` and
`:428`).

Correcting the table is the honest, cheap move if the adapters are not on the
roadmap: change the second column to name only what is implemented, and move
agy/Claude/Codex into the "no adapter — reminder only" row, in both language
versions. Then the 31 `pilot_wake_no_adapter` rows are expected behaviour
rather than evidence of a missing feature.

## Red-proof

A test that reads the adapter table out of `docs/PROTOCOL.md` and asserts it
matches `Pilot.WAKE_ADAPTERS` — or, if the table stays prose, a test that
asserts every role named as having an adapter has one. Before the fix, the
table names three CLIs with adapters and the code has one.

## Severity

medium — the contract document overstates what the system does, and the
evidence accumulates silently in the transcript.
