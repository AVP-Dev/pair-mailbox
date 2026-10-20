---
title: Five different rules resolve the state dir in one repo — the round-121 fix reached two of them
found: 2026-10-19T23:10Z
severity: medium
round: 126
status: open
---

## Where

| entry point | rule | file:line |
|---|---|---|
| `bin/pairup` | walk up from cwd, bounded by git root; a foreign inherited `PAIR_STATE` is refused (round 121) | `bin/pairup:45-70` |
| `mcp/pair_mailbox/serve.sh` | walk up from cwd inside a git repo; an env `PAIR_STATE` is discarded on a root mismatch (round 121) | `serve.sh:54-80` |
| `bin/pairpilot` | relative `.pair` from cwd, no walk-up, `allow_create=True` | `bin/pairpilot:2387`, `:207` |
| `mcp/pair_mailbox/pairctl_read.py` | explicit `--state`, then `PAIR_STATE`, then walk-up — its own order | `pairctl_read.py:141-161` |
| `bin/pair-duo` | inherited `PAIR_STATE` unconditionally, else `<install dir>/.pair` | `bin/pair-duo:33` |

## What breaks

Round 121's ruling — "the project root owns the state, never the inherited
environment" (`bin/pairup:29-44`, `serve.sh:35-53`) — is implemented in two
places and contradicted in three. Each un-migrated entry point is a way for a
pair to end up serving, or writing into, a room that is not the caller's.

The two concrete, reproduced consequences are their own cards:
`pair-duo-inherits-foreign-state.md` (a foreign room gets a writer) and
`pairpilot-creates-second-state.md` (a second room is created in a subdirectory).

The cost of the duplication is not the two bugs — it is that the rule has no
single place, so the next entry point will get it wrong the same way, and
"which room is mine?" can only be answered by reading five code paths.

## What to do

One resolver, used by all five. A module next to the state core
(`mcp/pair_mailbox/state_resolve.py`) exporting something like:

```python
def resolve_state_dir(*, explicit: str | None, env: Mapping[str, str],
                      cwd: Path, trust_env: bool = False) -> tuple[Path, str]:
    """Returns (room, reason). Never silent: a refusal names both rooms."""
```

`bin/pairup`, `serve.sh`, `bin/pairpilot`, `pairctl_read` and `bin/pair-duo` all
call it. The shell scripts call it through the same Python the server already
loads (`bin/pairup:119` already inserts `mcp/pair_mailbox` into `sys.path`), so
there is one implementation, not one per language.

The rule it encodes, unchanged from round 121:

- the project root (git top, or the nearest `.pair/state.json` walking up) is
  authoritative;
- an inherited `PAIR_STATE` is honoured only when it names THIS project's own
  room; anything else is refused loudly, with `PAIR_TRUST_ENV=1` as the
  explicit escape;
- an explicit `--state` on the command line is always trusted.

## Red-proof

A test that runs each of the five entry points from a subdirectory of a project
that has its own `.pair`, with an inherited `PAIR_STATE` pointing at a second
project, and asserts all five resolve to the same room. Before the fix, at
least `bin/pair-duo` and `bin/pairpilot` fail.

## Severity

medium — the two live consequences are already cards; this one is the root
cause, so fixing it closes those two as a side effect.
