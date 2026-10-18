# Round 106 slice C — one tmux server per project

The defect: a single tmux server (`/tmp/tmux-1000/default`) served every project
on the box, so another project's panes were visible and addressable from this
project's tools. Measured consequences: a message of ours once landed in
galatour's pane (round 104), and two projects sharing a basename would collide on
session name because `pairup` named sessions by `basename $PROJECT_DIR`
(bin/pairup:316).

## The shape (critic rulings, seq 1615)

- socket name `pair-<basename>-<hash8>`, hash AUTHORITATIVE and computed over the
  RESOLVED root path — never over the basename, and never read from state.json
  (binding a tree to a socket must not need the state file the socket protects).
  Two clones of one repo share basename AND origin and still hash apart.
- transition condition (b): the live pair must never be blinded. Three probes in
  order — (1) our own socket: reuse if a session with our deterministic name is
  already there (the idempotence half, ruling D1); (2) the default socket: reuse
  the legacy bare-basename session, because the roster's pane pointers already
  name a session the shared server can see; (3) neither: raise on our own socket.
  `PAIR_FORCE_SOCKET=1` skips 1 and 2 for tests and for forcing the migration.
- condition (c): ONE resolver. `composer._tmux()` and `server.tmux_command()`
  both return `tmux -L <socket>` when `PAIR_PANE_SOCKET` names one, plain tmux
  otherwise. pairup exports `PAIR_PANE_SOCKET` into the panes.

## Call-site audit — the list is closed (D2)

Every tmux invocation in the tree, and its fate:

| site | count | fate |
|---|---|---|
| `bin/pairpilot` via `from composer import _tmux` (self.tmux) | 23 call sites | converted — imports the same resolver; the mass-kill and send-keys paths inherit it |
| `mcp/pair_mailbox/server.py` (`shutil.which("tmux")`) | 4 | converted to `tmux_command()` (server.py:1104) |
| `mcp/pair_mailbox/server.py` literal `["tmux", "send-keys", ...]` | 1 | converted to `tmux_command().split() + [...]` |
| `mcp/pair_mailbox/pairctl_panes.py` | 1 helper | converted — hands a socket-aware tmux to composer's own pane probes |
| `bin/pairdoctor` | 1 | converted — imports composer._tmux() |
| `bin/pairrelaunch`, `bin/pairmcpdoctor`, `bin/pairmcprestart` (new tools) | 1 each | converted — `_tmux_cmd()`, same env read |
| `bin/pairsocket_test.py` | scratch projects only | deliberate: the tests must address BOTH namespaces to prove isolation |

Non-converted, with reason: nothing remains. Any new site must go through
`composer._tmux()` or `server.tmux_command()`; a second resolver is the
"most call sites" trap the critic named at seq 1615.

## What the red-proof exercises

`bin/pairsocket_test.py` (4 tests, green):
- a scratch project's session is invisible on the default server — the structural
  fix, proven rather than described;
- two same-basename projects on different roots hold sessions in SEPARATE
  namespaces — the collision class closed;
- the resolver prefixes under a socket, and degrades to plain tmux under
  `default` and with nothing set — one convention, three states;
- the name is readable AND hashed, 8 chars of hash.

`bin/pre_push_guard_test.py` and `mcp/pair_mailbox/test_composer.py` +
`test_panes.py` stay green (43 + guard), and `test_protocol.py` +
`test_receipt.py` 301 passed — the server-side change disturbs nothing.

## Human-facing attach line (D3)

`pairctl status` prints, for the current project:

```
tmux -L pair-<basename>-<hash8> attach -t pair-<basename>-<hash8>
```

so a human arriving cold attaches to the right server instead of hunting for a
session on a socket he was never told about.

## Known limits, stated

- a project that migrated mid-flight keeps its legacy session until the pair
  exits; forcing the move is `PAIR_FORCE_SOCKET=1` on the next `pairup`.
- the socket name lives in the environment of the panes and tools it started;
  a human's own terminal has no `PAIR_PANE_SOCKET`, so his plain `tmux` commands
  address the default server. That is why the attach line is printed, and why
  `bin/pairstate` reports the socket by name.
