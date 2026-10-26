---
title: bin/pairmcpdoctor ships with no tests and no gate rows
found: 2026-10-19T23:53Z
severity: medium
round: 126
status: open
---

> **Re-scoped in round 152.** This card used to cover BOTH `bin/pairmcpdoctor`
> and `bin/pairmcprestart`, and it claimed "neither appears in the verification
> profile or the change→suite map". Round 151 made half of that untrue:
> `bin/pairmcprestart_test.py` now exists (7 tests), `scripts/verification-profile.tsv`
> has an `mcpprestart` row, and `scripts/verify-map.tsv` maps both the tool and
> its test. That half is closed and is not repeated here.
>
> The half that SURVIVED is now its own card, with the measured detail:
> `docs/backlog/pairmcpdoctor-has-no-tests-and-still-sleeps-blind.md` — it
> carries the second blind-sleep convention, the weaker picker classifier, the
> missing `-J` capture, and the red-proofs.
>
> What is left HERE is the part that is not about a single tool: the structural
> rule that every shipped executable in `bin/` has a test, which is what let
> this gap exist and would let the next one exist.

## Where

- `bin/pairmcpdoctor` — MCP connection diagnostics for the roles' panes
- `scripts/verification-profile.tsv` — no row for `bin/pairmcpdoctor`
- `scripts/verify-map.tsv` — no mapping for either `bin/pairmcpdoctor` or a
  hypothetical `bin/pairmcpdoctor_test.py`
- the `bin/` convention: `pairboard` → `pairboard_test.py`, `pairdoctor` →
  `pairdoctor_test.py`, `pairpilot` → `pairpilot_test.py`, `pair-duo` →
  `pair-duo_test.py`, `pairup` → `pairup_test.py`, `pairrestart` →
  `pairrestart_test.py`, `pairmcprestart` → `pairmcprestart_test.py`
  (round 151), `pairmcpdoctor` → **nothing**

## What breaks

The convention in `bin/` is unbroken except for one file, and the gap is
invisible to every gate:

```
$ git ls-files bin/ | grep -E "mcp.*test"
bin/pairmcprestart_test.py
```

`pairmcpdoctor` is a SHIPPED operator of the transport layer — it reaches into
live tmux panes, takes a per-role `flock`, reads a CLI's `/mcp` picker and
toggles the pair server. A change in `cli-profiles.json`, in the tmux
interaction code, or in the MCP config format can break it with no signal from
any gate: the very breakage this tool exists to repair.

The card's own rule catches this today:

> A test that asserts every executable in `bin/` has either a `*_test.py`
> sibling or an explicit exemption recorded in the verification profile. Before
> the fix, `pairmcpdoctor` fails it.

That rule is still unwritten. It is the durable half of this card: the
per-tool half is filed separately, but the STRUCTURAL rule — every shipped
executable has a test or a named exemption — is what stops the next tool from
arriving the same way.

## What to do

- Write the structural test: every executable in `bin/` (excluding the
  `*_test.py` siblings themselves and anything explicitly exempted) has a
  `*_test.py` sibling OR an exemption line recorded in a checked-in file with
  a reason. The exemption list is checked in, so a new tool cannot be exempted
  by silence.
- Register the rule as a gate layer, next to the existing
  `verification-profile` and `verify-map` static checks, and map it in
  `scripts/verify-map.tsv` so a change to a `bin/` tool runs it.
- The per-tool work for `pairmcpdoctor` is in
  `docs/backlog/pairmcpdoctor-has-no-tests-and-still-sleeps-blind.md`.

## Red-proof

- Adding a new executable to `bin/` without a test sibling fails the new
  layer, and the failure names the file and the rule.
- Adding the exemption for a file requires a reason: an exemption with an
  empty reason fails.
- The layer passes on the current tree once `pairmcpdoctor` has its test (or
  a named exemption).

## Severity

medium — the gap is in a shipped operator of the fragile transport layer, and
the structural rule that would catch it is unwritten, so the next tool will
arrive the same way.
