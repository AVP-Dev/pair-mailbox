---
title: 10 of 11 test files in the `protocol` verification layer run zero tests
found: 2026-10-18
severity: high
round: 120
status: fixed
fixed: 2026-10-19
fixed_by: round 123
---

## Where
`scripts/verification-profile.tsv:30` (the `protocol` row) and each of
`mcp/pair_mailbox/test_protocol{,-authority,-gates,-notes,-delivery,-mailbox,-tui,-permissions,-panes,-transport}.py`.

## What will break
The row launches each file as `python3 <file>`. Ten of the eleven files have no
`if __name__ == "__main__": unittest.main()` block (`grep -c unittest.main`
returns 0; only `test_protocol_pins.py` has one), so each invocation imports the
module, defines its classes and exits with status 0 without running a single
test. `scripts/verify-project.sh:159-215` judges a check by its exit code only,
so the whole `protocol` layer reports green while executing nothing but one file.

Measured 2026-10-18 on HEAD `4246677`:
`python3 mcp/pair_mailbox/test_protocol_mailbox.py` → zero output, `rc=0`.

Consequences:
- CI green on every PR for a layer that covers authority, gates, notes, delivery,
  mailbox, TUI, permissions, panes and transport — the core protocol surfaces.
- Any regression in those areas lands undetected; "CI 5/5" stops being evidence.
- The gate also hides its own failure from the operator: no output looks like a
  fast suite, not a suite that never started.

## Why deferred
Found while investigating an unrelated token-cost complaint (an agent polling
`pair_read` in a loop). The immediate request was answered without this fix; the
change touches 10 test files and the round that would own them is not open. The
user was woken at night and asked for a scope decision — that ask was itself the
error; recorded here instead of re-asking.

## What to do
Pick one and apply it to the row, then prove it by breaking a test on purpose:

1. Add `unittest.main()` to the ten files (local fix per file, matches the one
   file that already has it), or
2. Change the row to launch through the loader (`python3 -m unittest
   mcp.pair_mailbox.test_protocol_mailbox ...`). Note the harness path first:
   `python3 -m unittest mcp.pair_mailbox.test_protocol_mailbox` fails with
   `ModuleNotFoundError: No module named 'mcp.pair_mailbox'` from the repo root,
   so this needs `PYTHONPATH=mcp/pair_mailbox` or per-file invocation with
   `sys.path` already set by the file itself.

Verification: after the fix, `python3 <file>` must print `Ran N tests` with
N > 0 for every file in the row, and `scripts/verify-project.sh --only protocol`
must fail when one test is deliberately broken.

## Fixed (2026-10-19, round 123)

Option 2 was applied: the row now launches through the loader, so one process
runs all eleven files and the layer's exit code means something.

```
protocol	positive	1	PYTHONPATH=mcp/pair_mailbox python3 -m unittest test_protocol test_protocol_authority ... test_protocol_transport	serial
```

The per-file `unittest.main()` blocks were NOT added: the loader needs none of
them, and ten extra blocks would have been ten more places to forget.

Measured after the fix:
- `PYTHONPATH=mcp/pair_mailbox python3 -m unittest test_protocol ...` →
  `Ran 297 tests ... OK (skipped=2)`, rc=0. Before: rc=0 with zero output and
  zero tests run.
- The layer goes red on a deliberately broken test: injecting one failing test
  into `test_protocol_pins.py` gave `FAILED (failures=1, skipped=2)`, rc=1. The
  broken test was removed again; the tree carries no trace of it.

While here: `mcp/pair_mailbox/test_read_delivery.py` had a `unittest.main()`
block but was in no profile row at all, so it never ran either. It now has the
`read-delivery` suite, and the map points both it and `read_delivery.py` at it.
