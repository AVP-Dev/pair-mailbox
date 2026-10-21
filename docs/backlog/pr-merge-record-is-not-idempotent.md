---
title: `pr merge-record` writes a second repair event when the PR was already repaired
found: 2026-10-21
severity: low
round: 139
status: open
---

## Where

- `mcp/pair_mailbox/review_gate.py` — `_cmd_pr_merge_record`
- `.pair/transcript-archive/3.jsonl` — the evidence: PR #61 carries two
  `pr_merge_repaired` events

## What breaks

The repair command for a merge that happened outside the gate appends an
event every time it runs. It checks the remote (the PR is MERGED, the claimed
head matches the merge commit GitHub reports) and then writes
`pr_merge_repaired` — but it never asks whether the journal already knows
about this merge. Round 115's repair of PR #61 ran twice, and the journal
holds two events for it.

Nothing breaks in the merge path: the second event is truthful (the merge
really happened, and the repair really ran). The trail is just no longer one
event per merge, and everything that reads it has to tolerate the duplicate —
the merge-trail audit (round 139) counts a PR as recorded when any of the
three record kinds is present, which is why the duplicate does not turn PR
#61 into a false escape.

## Acceptance

- `bin/pairctl pr merge-record <id>` on a PR the journal already holds a
  `pr_merge_repaired` event for either refuses, or reports that the PR is
  already recorded and writes nothing.
- A test: the second call leaves the number of `pr_merge_repaired` events for
  that PR unchanged.
