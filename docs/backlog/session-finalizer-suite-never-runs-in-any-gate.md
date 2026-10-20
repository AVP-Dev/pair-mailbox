---
title: The 69-test session-finalizer suite never runs — skills/ is mapped to the waiting-contract suite in both gates
found: 2026-10-19T23:49Z
severity: medium
round: 126
status: open
---

## Where

- `skills/session-finalizer/tests/run.sh` — the suite
- `scripts/verify-map.tsv:30` — `skills/	waiting-contract`
- `scripts/verify-map.tsv:33` — `.opencode/skills/	waiting-contract`
- `scripts/verification-profile.tsv` — no `session-finalizer` row (0 matches)

## What breaks

`skills/session-finalizer` is a full subsystem with its own 69-test suite
(`tests/test_apply.sh`, `test_check.sh`, `test_guards.sh`, `test_plan.sh`,
`test_report.sh`). No gate runs it.

- `scripts/verification-profile.tsv` has **no** row for it
  (`grep -c "session-finalizer" scripts/verification-profile.tsv` → 0).
- `scripts/verify-map.tsv:30` maps the whole `skills/` tree to
  `waiting-contract`. So when a file under
  `skills/session-finalizer/scripts/` changes, `verify-changed.sh` runs
  `test_waiting_contract.py` — a suite about the mailbox waiting contract, not
  about the finalizer — and the finalizer's own `run.sh` is never invoked.
- `scripts/verify-project.sh` (the full gate) does not include the suite
  either.

The 69 tests are not broken — `bash skills/session-finalizer/tests/run.sh`
gives 69 passed, 0 failed. They are simply never executed by anything the
project calls a gate. A regression in the finalizer ships green.

## What to do

- Add a `session-finalizer` row to `scripts/verification-profile.tsv` that runs
  `skills/session-finalizer/tests/run.sh`.
- Narrow the `skills/` mapping so `skills/session-finalizer/` routes to that
  suite and only the waiting-contract skills route to `waiting-contract`. A
  directory-wide mapping cannot be right for a directory that holds several
  independent subsystems.
- Keep the row in `verify-map.tsv` consistent with the profile, so
  `verify-changed.sh` picks the right suite for a changed finalizer file.

## Red-proof

Change one assertion in `skills/session-finalizer/tests/` (or break a
finalizer script) and run `scripts/verify-changed.sh` on that path: before the
fix, the changed-file gate passes. After the fix, the finalizer suite runs and
fails.

## Severity

medium — a complete, passing test suite that no gate reaches, which is the
quietest possible way for a subsystem to rot.
