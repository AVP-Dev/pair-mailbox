---
title: bin/pair-duo writes into a FOREIGN room inherited from PAIR_STATE, and defaults to the install dir instead of the project
found: 2026-10-19T23:05Z
severity: high
round: 126
status: open
---

## Where

`bin/pair-duo:33` — `STATE_PATH="${PAIR_STATE:-$ROOT/.pair}"`, where `ROOT`
(`bin/pair-duo:30`) is the **installation** directory (`$(cd -- "$HERE/.." && pwd)`),
not the caller's project. The value is then written into the panes at
`bin/pair-duo:164` (swap-critic) and `bin/pair-duo:290-291` (writer/critic init),
and used for state writes at `bin/pair-duo:225` (`pairctl --state "$STATE_PATH" task`)
and `bin/pair-duo:259` (`pairctl --state "$STATE_PATH" assign "$WRITER_ROLE"`).

## What breaks

Round 121 closed exactly this defect class — "a project owns its state dir, the
shell's env never repoints it" (commit `b7c68cb`) — but in two entry points only:
`bin/pairup:45-70` and `mcp/pair_mailbox/serve.sh:54-61`. `bin/pair-duo` is the
third entry into the same room and it was not covered.

Two distinct failures, both reproduced:

1. **A foreign room is written to.** With `PAIR_STATE=/tmp/foreign/.pair`,
   `bin/pair-duo agy` run from an unrelated directory appointed a writer in the
   foreign room and recorded the role in its roster:

   ```
   $ python3 -c "import json;d=json.load(open('/tmp/foreign/.pair/state.json'));\
       print('writer=',d.get('writer'),'roles=',d.get('roles'))"
   writer= agy-writer roles= {'agy-writer': {'first_seen': '2026-10-19T22:34:25+00:00',
   'reads': 0, 'sends': 0, 'last_seen': '2026-10-19T22:34:25+00:00', 'pid': 697129,
   'pid_start': 37058215}}
   ```

   The panes were then launched with `export PAIR_STATE='/tmp/foreign/.pair'`.

2. **The default is the install directory, not the project.** From
   `/tmp/myproj`, `bin/pair-duo` resolved `STATE_PATH=/mnt/storage/work/umputum/.pair`
   and `PROJECT_ROOT_DIR=/mnt/storage/work/umputum` — the pair-mailbox checkout
   itself, not the project the operator was standing in. `bin/pairup` searches
   upward from cwd (`bin/pairup:46-58`); `bin/pair-duo` searches nothing.

The direct invocation is the documented one: `docs/RUNBOOK.md:94`,
`docs/SETUP.md:57`, `docs/PROTOCOL.md:104` all point the operator at
`bin/pair-duo <cli>`. (`bin/pairup:351` calls `bin/pair-duo --swap-critic … --state
"$STATE_DIR" --project "$PROJECT_DIR"`, which is safe because the flags are
explicit — but that is not how a human starts a duo pair.)

## Why it was not fixed in round 121

The round-121 fix had two sites; the third entry point was not in scope of its
measurement. The repo now has **five** different rules for resolving the state
dir (`bin/pairup:46-58`, `serve.sh:65-80`, `bin/pairpilot:2387`,
`pairctl_read.py:141-161`, `bin/pair-duo:33`) — see the card
`state-dir-resolution-has-five-rules.md`.

## What to do

Apply the round-121 rule to `bin/pair-duo` as well:

- resolve the room by walking up from cwd (bounded by the git root), not from
  `$ROOT`;
- accept an inherited `PAIR_STATE` only when it names THIS project's own room;
  otherwise refuse loudly, with `PAIR_TRUST_ENV=1` as the deliberate escape
  (same wording as `bin/pairup:55-70`);
- default `--project`/`--state` to the caller's project, not the install dir.

## Red-proof

Run `bin/pair-duo` with `PAIR_STATE` pointing at a room that is not this
project's, in a temp dir:

```bash
mkdir -p /tmp/foreign/.pair && PAIR_STATE=/tmp/foreign/.pair bin/pair-duo opencode
```

Then assert two things. (a) The foreign `state.json` is byte-identical
before and after — the foreign room gains no `writer` and no role row.
(b) The refusal message names the foreign room, so the operator knows which
path was refused.

Before the fix, (a) fails: the foreign room gains a `writer` and a role row
(`pair-duo-inherits-foreign-state.md`, the reproduction output), and the
script exits 0.

A second assertion for the default: from a temp project that is NOT the
install dir, `bin/pair-duo` must resolve the room inside the temp project, not
inside the pair-mailbox checkout.

## Severity

high — a script writes another project's write authority and launches panes
bound to it, with no warning.
