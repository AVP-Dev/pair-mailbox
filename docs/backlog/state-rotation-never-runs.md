---
title: state_rotation.rotate() is never called in production — the "bounded hot file" is unbounded
found: 2026-10-19T23:15Z
severity: medium
round: 126
status: open
---

## Where

- `mcp/pair_mailbox/state_rotation.py:80` — `def rotate(state_dir, keep_tasks=None, keep_evidence=None)`
- `mcp/pair_mailbox/store_core.py:338` — the only production call site, and it
  calls `load_state`, **not** `rotate`
- `mcp/pair_mailbox/state_rotation.py:31` — `ROTATABLE = {"tasks": 40, "evidence": 40}`
- `mcp/pair_mailbox/state_rotation.py:1` — the docstring promise

## What breaks

The whole write half of the rotation mechanism is unreachable in production.
Every call to `rotate()` is from `mcp/pair_mailbox/test_state_rotation.py`
(lines 62, 72, 79, 80, 91, 100, 111) — nothing else:

```
$ grep -rn "state_rotation\." --include=*.py . | grep -v __pycache__ | grep -v test_
mcp/pair_mailbox/store_core.py:595:    from state_rotation import load_state
```

So the reader half (`load_state`, which merges an archive into the active
window) is wired, and the writer half is not. The archive is therefore never
created and the merge never has anything to merge.

Measured on the live room:

```
$ ls -la .pair/state.archive
ls: cannot access '.pair/state.archive': No such file or directory

$ python3 -c "import json;d=json.load(open('.pair/state.json'));\
    print('tasks',len(d['tasks']),'evidence',len(d['evidence']))"
tasks 130 evidence 53
```

`ROTATABLE` promises an active window of 40 records for each key; the live file
holds 130 tasks and 53 evidence records, and the count only grows — nothing in
the codebase trims `state["tasks"]` at all (there is no `del`/slice on the task
list anywhere outside `test_board_render.py` / `test_receipt.py` fixtures).

Two comments in the code now state something false:

- `store_core.py:332` — "Round 106 slice B: **the hot file is bounded**, so this
  load must hand back the UNION of the active window and the state archive."
  The hot file is not bounded; nothing bounds it.
- `state_rotation.py:1` — "State rotation: **the hot file stops growing
  forever** (round 106 slice B)."

## The cost

The whole file is rewritten under the global lock on every mutating call
(`store_core.py:343` `_save` → `store_core.py:552` `atomic_write_json`):

```
_save (137 KB atomic write) ms 5.71
Store.read ms 1.43
json.loads of the 137 KB state.json ms 1.144
```

At 130 tasks the file is 137 139 bytes, of which `tasks` alone is 65 297 and
`evidence` 22 776. Every `pair_state`, every `pair_read` and every evidence
record pays that write, and the cost grows with the round count.

## What to do

Either:

- **wire the writer** — call `rotate()` from `Store._save` (or from `mutate`)
  with the window from `ROTATABLE`, keeping the `load_state` union as the
  reader; the red-proof is the existing round-trip test plus a new one that a
  second `rotate()` does not double-count; or
- **delete the module** and correct the two comments, so the file stops
  promising a bound it does not have and `state.json` stops pretending to be
  bounded.

The first option is what round 106 slice B set out to do; the second is honest.
Leaving it as is means the next reader believes there is a rotation and looks
for its call site.

## Red-proof

Call `rotate()` today on a copy of a live `.pair` and observe `state.archive/`
appear and the active window shrink to 40 — then note that no production code
path reaches that call. The test that pins the fix: assert that after N
mutations the active window is bounded.

## Severity

medium — a whole mechanism that is 100 % dead in production, plus two
comments that assert a property the system does not have.
