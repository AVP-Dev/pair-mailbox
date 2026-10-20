---
title: bin/pairpilot creates a SECOND state dir when started from a subdirectory, and watches that one
found: 2026-10-19T23:12Z
severity: medium
round: 126
status: open
---

## Where

- `bin/pairpilot:2387` — `parser.add_argument("--state", default=os.environ.get("PAIR_STATE", ".pair"))`
- `bin/pairpilot:2431` — `state_dir = Path(args.state).resolve()`
- `bin/pairpilot:207` — `self.store = Store(state_dir, allow_create=True)` (the
  `Store` that creates the room is `store_core.py:221`)

## What breaks

`.pair` is a **relative** path here, so it resolves against the current working
directory, not against the project root. Every other entry point in the repo
walks up from cwd; `bin/pairup:14` states the rule explicitly:

> walk-up to `.pair/state.json` from cwd, same as in `serve.sh` and
> `pairctl_read.walk_up_to_state`: launching from a subdir never creates a
> second state.

`bin/pairpilot` breaks that rule, and `allow_create=True` turns the violation
into litter. Reproduced in a clean git repository:

```
$ cd /tmp/pilotprobe/sub && python3 /mnt/storage/work/umputum/bin/pairpilot --once
[01:55:41] pair session state — round 0 is in phase 'scoping' (fix)
  writer: UNASSIGNED
  task: NOT SET
  unread across roles: 0
  the supervisor is quiet by default: create ask to let it report into a CLI

$ ls -la /tmp/pilotprobe/sub/
drwxr-xr-x 3 alexios alexios   60 …  .
drwxr-xr-x 4 alexios alexios   80 …  ..
drwxr-xr-x 9 alexios alexios  220 …  .pair      <-- a second room, created
```

Two consequences:

1. The supervisor watches a room nobody is in. Its nudges, its wake files and
   its report all address the wrong state dir, so the pair looks silent to the
   pilot and the pilot looks busy to the pair.
2. The tree is polluted. The nearer `.pair` wins every later walk-up
   (`serve.sh:66-78`, `pairctl_read.walk_up_to_state`), so the second room
   **hides the real one** from the next tool that resolves by walking up.

## Why it was not fixed in round 121

Round 121 fixed the env-ownership half of the rule in `bin/pairup` and
`serve.sh`. The walk-up half of the rule was never applied to `bin/pairpilot` —
it has no walk-up at all, only a relative default.

## What to do

Resolve `--state` the way every other entry point does: walk up from cwd for
`.pair/state.json`, bounded by the git root, and refuse (rather than create) if
nothing is found. `allow_create=True` should stay only for the path that
legitimately bootstraps a room (`bin/pairup`).

## Red-proof

A test that starts `bin/pairpilot --once` from a subdirectory of a project that
has `.pair/state.json`, and asserts (a) no `.pair` appears in the subdirectory
and (b) the pilot's own state dir is the project's. Before the fix, (a) fails.

## Severity

medium — silent misdirection of the supervisor plus a second room that hides
the real one from every later walk-up.
