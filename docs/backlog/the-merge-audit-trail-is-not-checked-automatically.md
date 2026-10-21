---
title: A merge that escapes the sanctioned path is not detected - nothing compares the merged PRs with the journal's events
found: 2026-10-21
severity: medium
round: 138
status: open
---

## Where

- `bin/pairctl pr merge` — the sanctioned path (`mcp/pair_mailbox/review_gate.py`, `cmd_pr`)
- `.pair/transcript.jsonl` — the journal, where `pr_merged` / `pr_merge_forced` / `pr_merge_repaired` live
- `gh pr list --state merged` — what actually reached master

## What breaks

The journal records the merges that went through the sanctioned path. Nothing
compares it with the merges that actually happened. A hand-run `gh pr merge`
leaves no trace: the code reached master and the repo's own audit trail never
learned of it. Round 115 found that after the fact (456 rows, zero merge
events) and the fix was the rule in PROTOCOL §10g plus the repair command.

Measured on the round-138 tree (2026-10-21): the journal (the active file
plus the rotated archive) holds merge events for PRs 61-84, except PR 76
(closed unmerged). PRs 1-60 hold none. The journal was rotated before
2026-10-20, so for the older merges "never recorded" cannot be told apart
from "rotated away" — which is exactly why the check has to be mechanical and
the horizon has to be written down.

The horizon, as far as it is known today: the journal learned to record a
merge with PR #27 (the commit that added the event), so PRs 1-26 predate the
trail — debt, not a violation. PRs 28-60 (33 merges) came after the trail
existed and are not in it. Round 138 decided NOT to write the repair events
for them: 33 rows stamped with today's date would land in the tail of the
live journal and distort its chronology, and the decision on the horizon
belongs here, not to a repair run in passing. The facts are recorded in
`docs/rounds/138.md`.

## What to do

A script that asks GitHub which PRs are merged and compares them with the
journal's merge events, naming each PR that reached master without a trace.
It needs one decision first: the horizon. The pre-gate era — the merges
before the journal could record them — is debt, not a violation, and the
script must say which is which instead of counting both the same way.

The repair already exists (`bin/pairctl pr merge-record`): it reads the
remote, refuses a mismatch, and writes `pr_merge_repaired`, so the script's
job is to find the escapes and the repair closes them.

## Red-proof

Merge a branch with a hand-run `gh pr merge` and run the script: it names the
PR. With the comparison removed, the script is green on a repository whose
audit trail has a hole.

## Severity

medium — the sanctioned path works and the rule is in the protocol; this is
about the escapes nobody looks for by hand.
