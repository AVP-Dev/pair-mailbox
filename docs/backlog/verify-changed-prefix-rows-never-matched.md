---
title: verify-changed.sh's directory rows never matched — a change under skills/ or scripts/ selected NO suite, and the gate said so with an empty list
found: 2026-10-20T07:50Z
severity: medium
round: 129
status: fixed
fixed: 2026-10-20
fixed_by: round 129
---

## Where

- `scripts/verify-changed.sh:29` (before the fix) — the selector
- `scripts/verify-map.tsv:31,32,34,103,104` — the directory rows it failed to reach
- `mcp/pair_mailbox/test_verify_map.py` — the guard that now pins the selection

## What breaks

The map's rows are either an exact path (`bin/pairpilot_test.py`) or a DIRECTORY
PREFIX written with its trailing slash (`skills/`, `.agents/`,
`.opencode/skills/`, `scripts/`, `docs/`). The selector was:

```bash
suite=$(awk -F'\t' -v f="$f" '$f == $1 || index($f, $1"/") == 1 {print $2; found=1} END {if (!found) print "UNKNOWN"}' "$MAP" | head -1)
```

Two mistakes, and each alone is fatal:

- **`$f` in awk is FIELD f, not the variable `f`.** `f` holds the path string,
  so `$f` is the whole record. The comparison was therefore between the whole
  line and the first field — never a path comparison.
- **`index($f, $1"/")` appends a slash to a prefix that already ends in one.**
  The row says `skills/`, so the probe looked for `skills//`. No file path
  contains that.

The exact rows worked, because the SECOND awk (`$1 == f`) compared the field to
the variable correctly. Every DIRECTORY row was dead.

## The symptom was silence

Measured 2026-10-20, on the round-129 tree:

```
$ printf '\n<!-- probe -->\n' >> skills/pair-work/SKILL.md
$ ./scripts/verify-changed.sh
verify-changed: files: skills/pair-work/SKILL.md
verify-changed: suites:
```

An empty suite list. The gate then ran `verify-project.sh --only static` and
nothing else — and exited 0. So a change to any file under `skills/`,
`.agents/`, `.opencode/skills/`, `scripts/` or `docs/` passed the changed-file
gate having run NO suite at all, while the map's directory rows read as if they
were covered.

The count: 87 tracked files whose ONLY mapping is a directory row. Every one of
them ran nothing.

This is the same shape the audit round found elsewhere: a check whose green
claims more than it checked. `docs/backlog/session-finalizer-suite-never-runs-in-any-gate.md`
noticed that the finalizer's own suite never runs; this card names the reason
the `skills/` row never ran either — the row was never read.

## What to do

The selector was rewritten to compare the VARIABLE and to use the prefix as
written:

```bash
suite=$(awk -F'\t' -v f="$f" 'f == $1 || index(f, $1) == 1 {print $2; found=1} END {if (!found) print "UNKNOWN"}' "$MAP" | head -1)
```

and moved into a `suite_for()` function, so the probe and the loop cannot
disagree. The follow: pin handling moved with it.

`--suite-for PATH` was added: it asks the real script which suite a path
selects and prints it. That is what makes the guard possible without a second
implementation of the rule.

## Red-proof

`TestVerifyChangedRouting` in `mcp/pair_mailbox/test_verify_map.py` asks the
real script (`--suite-for`) and asserts the selection:

- a file under `skills/`, `.agents/` or `.opencode/skills/` reaches
  `waiting-contract` — the suite the map's row promises;
- `scripts/verify-map.tsv` reaches `verify-map` and NOT `scripts-check` — an
  exact row beats the directory prefix.

With the old rule restored, the test fails and names the promise it found
broken:

```
AssertionError: 'waiting-contract' not found in '' : skills/pair-work/SKILL.md
must reach the waiting-contract suite — the map's skills/ row promises it
```

3 of the 5 tests fail. With the fix, 5 pass.

## The other half: what the fix exposed

Repairing the rule did not only wake the directory rows up. It also made 47
tracked files resolve to `UNKNOWN` — files with no row at all
(`bin/pairctl`, `mcp/pair_mailbox/board_render.py`, `mcp/pair_mailbox/plan_gate.py`
and 44 more; 30 of the 47 are source files). Under the broken rule those files
ALSO selected nothing; the `UNKNOWN` fallback ("no mapping, running the whole
positive") is in the script and was unreachable, because the first awk always
found a match (a comment line) and printed an empty suite.

They are now honestly `UNKNOWN`: a change to one of them runs the whole
positive layer instead of nothing. That is slower and it is correct — the gate
says it cannot pick a suite and falls back to running everything.

Two of them are in this round's own change set (`install_test.py`,
`scripts/verify-map.tsv`), so both got exact rows. The remaining 47 are
tracked in `docs/backlog/verify-map-missing-rows-for-47-files.md`.

Note the direction of the fix. Measured on the same tree, before and after:

- before: 134 tracked files selected an EMPTY suite — 87 whose directory row
  was dead, and 47 with no row at all. All 134 ran static and nothing else.
- after: 0 select an empty suite. 87 reach the suite their directory row
  promises, and the 47 with no row say `UNKNOWN` out loud, which makes the
  gate fall back to the whole positive layer.

## Severity

medium — the changed-file gate was a no-op for 97 tracked files, and it said so
with an empty suite list that reads like "nothing more to run" rather than
"nothing was selected".
