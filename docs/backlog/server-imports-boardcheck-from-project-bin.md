---
title: server.py grows sys.path on every pair_state and imports the board checker from the PROJECT's bin/ — so the board gate never runs outside pair-mailbox
found: 2026-10-19T23:22Z
severity: medium
round: 126
status: open
---

## Where

`mcp/pair_mailbox/server.py:1534-1541`, inside `_done_blockers`:

```python
board = self.store.root / "plan.md"
if board.exists():
    try:
        sys.path.insert(0, str(self.store.project_root / "bin"))
        import boardcheck as _boardcheck
        _ok, _problems = _boardcheck.check(board, require_ancestor=False)
```

## What breaks — two defects in four lines

### 1. `sys.path` grows without bound

The `insert` is inside the function, not at import time, and the function runs
on every `pair_state`. Measured in a clean process:

```
initial len 45
0 len 46   1 len 47   2 len 48   3 len 49   4 len 50   5 len 51
```

One entry per call, no cap, no removal. With the state cache on (the default)
the growth stops after the first call, because the cached answer short-circuits
before `_done_blockers`; with `PAIR_DISABLE_STATE_CACHE=1` — and on every state
change, which is every real round — it grows on every call.

### 2. The board gate does not run in any project but pair-mailbox itself

`import boardcheck` resolves against `self.store.project_root / "bin"` — the
**user's project**, not the pair-mailbox installation. `boardcheck.py` ships in
the pair-mailbox checkout at `bin/boardcheck.py`, so the import only succeeds
when the project being audited *is* pair-mailbox.

Reproduced on a clean project with a `plan.md` present:

```
[pair-mailbox] board check skipped (No module named 'boardcheck')
```

and no board blocker appears in `_done_blockers`. The board leg is described in
the code right above as a machine gate that "stops being a human's job"
(`server.py:1527-1532`) — in every real project it is simply absent, and the
absence is a log line nobody reads.

### 3. The same path executes project-supplied code in the server process

Because the import is by name from the project's `bin/`, any `boardcheck.py`
placed there is executed by the MCP server on the first `pair_state`. Verified
by planting a file in a test project:

```
$ cat /tmp/plantproj/bin/boardcheck.py
import pathlib
pathlib.Path('/tmp/PLANTED_EXECUTED').write_text('executed by server process')

$ … _done_blockers(state) …
$ ls -l /tmp/PLANTED_EXECUTED
-rw-r--r-- 1 alexios alexios 46 … /tmp/PLANTED_EXECUTED
```

This is not a privilege escalation on its own — whoever can write into the
project can already run code — but it is an unexpected execution channel into
the long-lived server process, and it makes a security-relevant gate depend on
the content of the audited tree.

## What to do

- Import the checker from the **installation**, not from the project: the server
  already puts its own package dir on `sys.path` (`server.py:54`); add the
  shipped `bin/` once at import time, and import `boardcheck` by that path.
- Do the `sys.path.insert` at module import, once, guarded by a membership
  check — never inside a per-call code path.
- Decide what the board gate is supposed to do for a project that has no
  `bin/boardcheck.py`: if it must run everywhere, ship the checker with the
  server; if it only applies to pair-mailbox's own rounds, say so in the
  comment instead of letting it fail open with a log line.

## Red-proof

A test that builds a project with a `plan.md` and NO `bin/boardcheck.py`, calls
`_done_blockers`, and asserts that the board check actually ran (or that the
skip is a loud refusal rather than a silent one). Before the fix, the check
never runs.

## Severity

medium — an unbounded global list, plus a documented machine gate that is dead
in every deployment that is not the tool's own repository.
