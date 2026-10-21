---
title: the pilot's ring is a status-line popup, so it never reaches a full-screen TUI agent
found: 2026-10-21T18:45Z
severity: high
round: 141
status: open
---

## Where

`bin/pairpilot:792-812` — method `ring`. The neighbour's pane is woken with one
`tmux display-message -t <pane> -d 4000`, nothing else. The adapter
(`WAKE_ADAPTERS`, `bin/pairpilot:790`) is a separate, second attempt.

The server's own ring is a different path and does type into the composer:
`mcp/pair_mailbox/server.py:1891-2010` (`_ring`).

## What breaks — measured, on a live OpenCode pane

```
tmux display-message -t 56:0.1 -d 4000 "[pair] PROBE popup from umputum"
tmux capture-pane -p -t 56:0.1 | grep "PROBE popup"
  -> popup visible in pane capture: False
```

The popup is not pane content: `display-message` draws in tmux's message area
outside the pane's rendered region. A full-screen TUI (OpenCode, agy) owns that
region and repaints it, so the neighbour never reads the text. The agent reads
its own screen buffer; the message is not in it.

The same pane accepts the composer path fine — measured:

```
composer_line(56:0.1)      -> '  ┃  Ask anything… "Fix broken tests"'
is_composer_empty(...)      -> True
ring_refusal_reason(...)    -> None
deliver(pane, pointer, submit=False) -> delivered: True
```

So the capability exists; the pilot's ring simply does not use it.

## Why it matters

The supervisor's whole escalation ladder ends at that popup: `ring()` is called
from `poll_wakes` / `wake` (`bin/pairpilot:759-790`) and from the nudge path.
For every CLI whose screen is not the terminal's plain text — OpenCode first,
agy too — the wake is a no-op, and the room looks "silent" while the peer is
merely unaware.

The umputum room's own pilot log shows the resulting shape: a wake file per
role, and `pilot_wake_pending` / `pilot_wake_api` / `pilot_push_quiet` with no
peer ever reacting.

## What to do

Give `ring()` the same composer path the server uses, with the same guards
(empty composer, own pane, not foreign), and keep `display-message` as the
fallback for a pane that refuses. Reuse `composer.deliver` and
`composer.ring_refusal_reason` rather than a second implementation.

## Red-proof

A test that puts a fake full-screen TUI in a tmux pane, rings it through
`ring()`, and asserts the peer pane received text — today it receives none.
