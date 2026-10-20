---
title: Dead code across mcp/pair_mailbox and bin/ — seven never-called functions, one of them a red-proof that proves nothing
found: 2026-10-19T23:38Z
severity: low
round: 126
status: open
---

## Where

| function | file:line | references |
|---|---|---|
| `canonical_pane` | `mcp/pair_mailbox/composer.py:218` | 0 (one mention, in a comment saying it is NOT used) |
| `is_input_box_row` | `mcp/pair_mailbox/composer.py:537` | 0 |
| `reset_heal_state` | `mcp/pair_mailbox/heal.py:40` | 0 |
| `tamper_probe` | `mcp/pair_mailbox/pair_notes_consistency.py:202` | 0 |
| `server_code_mtime` | `bin/pairrestart:145` | 0 (one mention, in a comment explaining why it must not be used) |
| `artefact_at` | `bin/boardcheck.py:59` | 0 |
| `_sha256` | `bin/pairsocket_test.py:25` | 0 |
| `state_rotation.rotate` | `mcp/pair_mailbox/state_rotation.py:80` | tests only — see its own card |

## What breaks

Nothing crashes. The cost is that the code lies to the next reader, and in two
cases the lie is specifically about a protection.

### `canonical_pane` (composer.py:218)

Its docstring reads as a hard requirement:

> **Backward compatibility here is mandatory, not polite**: live roles carry
> `%N` in state, and had the record stopped taking them, the upgrade would
> break delivery for all at once.

The single reference to it in the repo is `server.py:1060`, and it says the
opposite of what a reader would conclude:

> Exactly `session_address`, not `canonical_pane`: the latter returns the raw
> input on refusal — reads as success — leaving "no address" nothing to notice
> by.

So the function that documents a mandatory compatibility rule is not the one in
use, and the one in use is mentioned only in a comment. A reader hunting for
"where do we still record `%N`" finds a function that nothing calls.

### `tamper_probe` (pair_notes_consistency.py:202)

The docstring says:

> The leg's own red-proof shape, so the probe and the production check agree.
> Builds an entry in the header format WITHOUT a matching journal row — the
> exact shape of a note hand-written into a kind file. A caller with a state
> dir containing this entry and no such row must refuse naming it.

It builds nothing. The whole body is a `return` of a constant:

```python
return False, (f"{kind}.md: entry {stamp} — {role} carries the entry format but no "
               f"journal row exists for it — this is the shape the leg refuses")
```

This is the pattern the audit skill names explicitly (§5, "A comment without a
test is a finding"): a function that exists to be a red-proof, is never called,
and cannot fail — it returns the same string whatever it is given. The
`check_notes` it claims to be the twin of (`pair_notes_consistency.py:99`) lives
on its own and is not exercised by it.

### `reset_heal_state` (heal.py:40)

The only function that would reset the self-heal counter, with no caller — and
its absence is what makes the counter cumulative. See the card
`heal-counter-is-cumulative-not-consecutive.md`.

### `server_code_mtime` (bin/pairrestart:145)

The function whose use caused the 24-hour outage documented at
`bin/pairrestart:66-83`:

> Measured root cause of the 24h outage pattern: `server_code_mtime()` took the
> newest mtime across ALL `mcp/pair_mailbox/*.py`, tests included, so every
> commit touching a test file made EVERY role's server stale, and `--yes` then
> killed the peer's server too.

The replacement `runtime_code_mtime` sits directly above it and is what
`diagnose()` uses (`bin/pairrestart:244`). The broken one is still callable,
one line away, with no test that would notice.

### The rest

`is_input_box_row` (composer.py:537) — the input-field recogniser, replaced by
`is_input_box_border`; `artefact_at` (boardcheck.py:59) — the by-sha artefact
check, replaced by `artefact_at_head`; `_sha256` (pairsocket_test.py:25) — an
unused digest helper in a test file.

## How it was measured

AST pass over every top-level `def` in `mcp/pair_mailbox/*.py` and `bin/*`,
cross-referenced against every `Name`/`Attribute`/alias/string occurrence in
the whole repository (all `.py`, `.sh`, `.md`, `.json`, `.js`, `.tsv` files
outside `.git`), then each survivor confirmed by `grep -rn` over the whole
tree — `mcp/`, `bin/`, `scripts/`, `docs/`, `skills/`, `README.md`,
`install.sh`, `finish.sh`. `cmd_*` functions were excluded: they are dispatched
by name from the `pairctl_cli` table (`pairctl_cli.py:52-78`).

## What to do

Delete them, except where a card above says the function should be revived
(`reset_heal_state`) or where the history is worth keeping in prose
(`server_code_mtime` — the comment at `bin/pairrestart:66-83` already carries
the whole story).

For `tamper_probe`: either delete it, or make it a real red-proof inside
`test_pair_notes_consistency.py` — write a note through `pair_note`, delete its
journal row, and assert `check_notes` names it. The function as written cannot
fail, so a test calling it would prove nothing.

## Red-proof

A test that asserts the dead set is dead: parse `mcp/pair_mailbox/*.py` and
`bin/*` with `ast`, collect the top-level `def` names, and assert that each of
the seven has no live reference outside its own definition (excluding the
`cmd_*` dispatch table). Before the fix, the assertion fails on all seven; the
moment one of them is wired up, the test names it.

For `tamper_probe` specifically, the red-proof is behavioural, not
structural: write a note through `pair_note`, delete its journal row, and
assert `check_notes` names it. Before the fix, the function as written returns
`False` and a fixed string for every input, so it cannot detect the tampered
row it claims to detect.

## Severity

low — but this is the "dead and redundant code" third of the audit brief, and
two of the seven are actively misleading about a protection.
