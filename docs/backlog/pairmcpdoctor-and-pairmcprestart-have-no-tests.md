---
title: bin/pairmcpdoctor and bin/pairmcprestart ship with no tests and no gate rows
found: 2026-10-19T23:53Z
severity: medium
round: 126
status: open
---

## Where

- `bin/pairmcpdoctor` — MCP connection diagnostics for the roles' panes
- `bin/pairmcprestart` — restarts a role's MCP server through the agent's
  `/mcp` interface
- `scripts/verification-profile.tsv` — no row for either
- `scripts/verify-map.tsv` — no mapping for either

## What breaks

```
$ git ls-files bin/ | grep -E "mcp.*test"
(no output)
```

Zero tests for either tool, and neither appears in the verification profile or
the change→suite map. The convention in `bin/` is otherwise unbroken:
`pairboard` → `pairboard_test.py`, `pairdoctor` → `pairdoctor_test.py`,
`pairpilot` → `pairpilot_test.py`, `pair-duo` → `pair-duo_test.py`,
`pairup` → `pairup_test.py`, `pairrestart` → `pairrestart_test.py`. These two
are the exception.

Both tools touch the most fragile surface the project has — live tmux panes and
the per-CLI MCP configuration:

- `pairmcpdoctor` inspects each role's pane and its MCP connection;
- `pairmcprestart` reaches into an agent's `/mcp` interface to restart the
  server.

A change in `cli-profiles.json`, in the tmux interaction code, or in the MCP
config format can break both tools with no signal from any gate — the class of
breakage these tools exist to repair.

## What to do

- Add `bin/pairmcpdoctor_test.py` and `bin/pairmcprestart_test.py`, following
  the pattern of `bin/pairdoctor_test.py` (a fake/stub CLI, a temp state dir,
  assertions on the observable outcome — not on internal calls).
- Add both to `scripts/verification-profile.tsv` and to `scripts/verify-map.tsv`
  so a change to either is mapped to its own suite.
- Decide explicitly whether `bin/split_protocol_tests.py` (see
  `verify-map-tsv-names-two-files-that-no-longer-exist.md`) is a shipped
  executable or a one-shot script; the two `*mcp*` tools are shipped
  executables and need the same treatment.

## Red-proof

A test that asserts every executable in `bin/` has either a `*_test.py`
sibling or an explicit exemption recorded in the verification profile. Before
the fix, `pairmcpdoctor` and `pairmcprestart` fail it.

## Severity

medium — two shipped operators of the transport layer with no safety net, in a
repository whose own convention says they should have one.
