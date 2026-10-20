---
title: The self-heal cap counts attempts over the whole project lifetime, not consecutive ones — PROTOCOL says "consecutive"
found: 2026-10-19T23:18Z
severity: medium
round: 126
status: open
---

## Where

- `mcp/pair_mailbox/heal.py:20` — `HEAL_STATE_FILE = "heal-state.json"`
- `mcp/pair_mailbox/heal.py:40` — `def reset_heal_state(state_dir)` — **never called**
- `mcp/pair_mailbox/heal.py:55` / `:93` — the only read and the only write of the counter
- `mcp/pair_mailbox/heal.py:58-61` — the cap check
- `docs/PROTOCOL.md:802` (§10f) — the contract

## What breaks

`MAX_HEAL_ATTEMPTS = 3` is described in the protocol as a **consecutive**
cap:

> Flapping safeguard: maximum **consecutive** auto-heal attempts
> (`MAX_HEAL_ATTEMPTS = 3`). Exceeding the ceiling halts auto-reopening and
> requires human intervention.

The implementation counts attempts over the **entire lifetime of the project**.
The counter is only ever incremented:

```
$ grep -rn '"attempts": 0' --include=*.py mcp/ bin/ | grep -v __pycache__
mcp/pair_mailbox/heal.py:31:    return {"attempts": 0, "last_heal": None}   # default when the file is absent
mcp/pair_mailbox/heal.py:41:    save_heal_state(state_dir, {"attempts": 0, "last_heal": None})   # inside the dead reset_heal_state
```

There is no third place. `reset_heal_state` — the one function that would
restore the counter — has no caller anywhere in the repository:

```
$ grep -rn "reset_heal_state" --include=*.py . | grep -v __pycache__
mcp/pair_mailbox/heal.py:40:def reset_heal_state(state_dir: Path) -> None:
```

Consequence: the third failure of any kind, **at any point in the project's
history**, permanently disables self-healing for that room. `heal.py:61-64`
returns `{"healed": False, "exhausted": True}` and writes a `heal_exhausted`
row into the transcript, and the only way back is for a human to delete
`.pair/heal-state.json` by hand.

Nor does `pairctl reset` help: it removes `mailbox_root`, `notes_root`,
`patches_root` and `state_path` (`pairctl_authority.py:384-385`) — but
`heal-state.json` lives in the state dir root and is not in that list, so the
counter survives a full reset.

`mcp/pair_mailbox/heal.py:9` repeats the same promise in prose: "Guards against
an endless self-heal loop via the attempt cap `MAX_HEAL_ATTEMPTS = 3`."

## What to do

Make the counter mean what the protocol says it means:

- reset `attempts` when a round closes successfully (the natural place is the
  `done` transition, or the start of a new round) — that makes "consecutive"
  true and gives `reset_heal_state` a caller;
- make `pairctl reset` remove `heal-state.json` with the rest of the room, so
  a human reset really resets;
- keep the transcript row either way, so the exhaustion is auditable.

## Red-proof

A test that records three failing evidences in three **separate, successful**
rounds and asserts that the fourth round still self-heals. Before the fix, the
fourth round reports `heal_exhausted`.

## Severity

medium — a documented, self-healing system silently becomes unrecoverable
after three incidents in a project's whole life, and the operator has no
supported way to restore it.
