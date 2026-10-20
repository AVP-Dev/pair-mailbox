---
title: state.json grows without bound, and the rotation mechanism that was meant to bound it needs a design round, not a wiring
found: 2026-10-20T05:20Z
severity: medium
round: 127
status: open
---

## Where

- `mcp/pair_mailbox/store_core.py:343` — `_save` writes the whole state
- `mcp/pair_mailbox/store_core.py:338` — the union merge that was removed
- the live room: `.pair/state.json` at 144 568 bytes

## What breaks

`state.json` grew to 144 568 bytes in 127 rounds, and nothing bounds it:

```
tasks          list  n=131       66365 bytes   45.9%
evidence       list  n=57        27986 bytes   19.4%
patches        dict  n=15         9380 bytes    6.5%
verdicts       list  n=20         8413 bytes    5.8%
```

Every mutating call rewrites all of it under the global lock
(`store_core.py:343` `_save` → `store_core.py:552` `atomic_write_json`), so the
cost grows with the round count.

Round 126 found the mechanism that was supposed to prevent this
(`state_rotation.py`) and found it 100 % dead in production. Round 127 measured
what happens if the writer is simply wired — and it does not work, for two
independent reasons.

### 1. The two halves of the mechanism contradict each other

`rotate()` assumes `state.json` holds ONLY the active window: it computes
`merged = known_archive_rows + active_rows` and moves the overflow out. But
`_load` hands the caller the UNION (`known + active`), and `_save` writes that
union back into the same file. So the next `rotate()` counts the already
archived rows a second time.

Measured on a copy of the live room, 131 real task records:

```
after rotate:            file 40 tasks,  archive  91 rows
after one mutate:        file 131 tasks              <- the union was written back
after second rotate:     file 40 tasks,  archive 182 rows
UNION task records: 222   <-- real is 131
DUPLICATION: 91
```

That is 91 duplicated task records, each one a second entry with the same id.

### 2. The union-on-read makes reads slower, not faster

The archive does not take the history off the read path — it puts it back on
it, because a reader must see every task. Measured on a synthetic room with
1 048 tasks:

| | read (median) | write (median) |
|---|---|---|
| one file, no archive (today) | 10.36 ms | 1.60 ms |
| rotated, union on read | 14.69 ms | 9.75 ms |
| rotated, hot file only | 0.65 ms | — |

The hot file alone is 17x faster to read. The union is what every reader
actually pays, and it is slower than the single file it replaced. The write
gets slower too, because the union is written back.

So the mechanism as designed moves the cost from "one big file" to "one big
file plus a copy of it", and pays an extra merge on every read.

### 3. The promise it makes is not the promise it keeps

`state_rotation.py:1` says "the hot file stops growing forever" and
`store_core.py:332` says "the hot file is bounded". Neither is true today, and
wiring the writer as the round-126 card proposed would make the first true
while making the reads worse and duplicating records on the way.

## What to do

The mechanism needs a design round, not a wiring. The design question is the
one the current code answers wrongly:

**Which records does a READER need?** Today every reader gets the union, so the
archive saves nothing on the read path. A design that works separates the two
kinds of state:

- the ACTIVE window — open tasks, the current round's evidence, the live
  roster — stays in `state.json` and is bounded;
- the HISTORY — closed tasks, past evidence — leaves the hot file entirely,
  and the few readers that need it (the board, the manifest, `pairctl tasks`)
  read it from a place that is not on the hot path.

Then `_save` writes only the active window, the archive is rewritten (not
appended) from the in-memory union so a mutation to an archived record is not
lost, and `_load` does NOT union — the union becomes an explicit read for the
readers that ask for it.

That is a change to the store's read path and to every consumer of the task
list, which is why it does not belong in a round that also fixes a syntax gate
and a dead-code sweep.

### Red-proof

A test that asserts the real property, not the old one:

- after N mutations the ACTIVE window in `state.json` is bounded (today it is
  not: 131 tasks, 144 568 bytes);
- the task list a reader sees still contains every task, with the same ids and
  the same count (no duplication — the round-127 measurement produced 222 union
  rows for 131 real records);
- a reader that only needs the active state does not pay for the history
  (the hot-file-only read measured 0.65 ms against 14.69 ms for the union).

## Severity

medium — the file grows without bound and every mutation pays for the whole
history, but nothing breaks today and the growth is linear, not explosive.
