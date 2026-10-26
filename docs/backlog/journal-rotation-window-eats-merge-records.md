---
title: The journal's rotation window silently eats merge records — ten were lost and had to be repaired by hand
found: 2026-10-26T22:40Z
severity: medium
round: 151
status: open
---

## Where

- `mcp/pair_mailbox/store_core.py:43` — `TRANSCRIPT_ARCHIVES = 4`
- `mcp/pair_mailbox/store_core.py:478-485` — the rotation shift, which deletes
  the oldest archive outright
- `mcp/pair_mailbox/store_core.py:41` — `TRANSCRIPT_MAX_BYTES = 256 * 1024`
- `scripts/merge-audit-trail.py:82-93` — `journal_files()`, which reads the
  active file plus every surviving archive
- `scripts/merge-audit-trail.py:96-115` — `journal_pr_merged()`, which only
  knows what the surviving files hold

## What breaks

The journal rotates. When the active file passes 256 KB it is shifted to
`transcript-archive/1.jsonl`, the older archives are pushed down, and the
oldest one is **deleted** (`store_core.py:482-483`). Four archives of 256 KB
each is the whole window — about 1 MB of history, roughly five days of this
project's activity.

Every merge record is a row in that journal. So when a merge event rotates
out of the window, the merge trail loses it — silently. The audit then
reports the merge as an ESCAPE, and the correct fix (`pairctl pr
merge-record`) is no longer available, because the repair command writes a
new event for a merge that the remote still reports as MERGED but that the
journal no longer knows about.

**This is not hypothetical: it happened, and ten merges were lost.** Round
151's full gate failed on `scripts/merge-audit-trail.py`: 97 merged PRs, 17
with a merge record, 80 without. Ten of the missing ones (PRs 72-75, 77-82)
were merged between 2026-10-20T06:52:50Z and 2026-10-21T08:56:02Z, and the
surviving journal now begins at **2026-10-21T09:03:18Z** — the merge of PR
83 is the first event that survived.

Measured window today (round 151):

| archive | rows | span | merge events |
|---|---|---|---|
| `transcript.jsonl` (active) | 496 | 2026-10-26T20:31 .. 23:18 | 11 |
| `transcript-archive/1.jsonl` | 944 | 2026-10-25T22:28 .. 10-26T20:31 | 7 |
| `transcript-archive/2.jsonl` | 1025 | 2026-10-21T20:53 .. 10-25T22:28 | 4 |
| `transcript-archive/3.jsonl` | 988 | 2026-10-21T09:03 .. 10-21T20:53 | 6 |

Note there is no `4.jsonl`: with `TRANSCRIPT_ARCHIVES = 4` the shift loop runs
from index 3 down to 1, and index 3 is the file that gets deleted. Three
surviving archives plus the active file is the whole reachable history, and
`3.jsonl` is already at 262 321 bytes against a 262 144-byte cap — the next
rotation deletes it.

The ten repairs were written on 2026-10-26 as `pr_merge_repaired` events in
the active file. They are truthful — the remote's merge state was read and
confirmed — but they are **written now for merges that happened then**, and
that is only defensible because the protocol has a command for exactly this
case (§10g.6). A merge that rotates out and is never noticed becomes a
permanent false escape in the audit.

## Why the window cannot just be widened

Widening `TRANSCRIPT_ARCHIVES` trades disk for history and costs nothing
per read, because `journal_pr_merged()` already walks every archive file. It
is not obviously wrong — but it is a **blind** widening: the number that
would be right depends on the merge rate, and nothing measures that. At the
observed rate of roughly one merge every two hours, a 4-archive window holds
about five days and a merge older than that is unrecoverable-by-rotation.

There is a second, sharper problem, and it is the one that makes this a
design question rather than a constant bump:

**The rotation window and the merge trail have opposite requirements.** The
journal rotates because the active file must stay small (the tail-read path
and `pairctl log` both care about its size). The merge trail must remember
every merge FOREVER — it is the audit's entire ground truth, and a merge
that falls out of the window is indistinguishable from a merge that escaped
the gate. One file cannot serve both: the thing that is safe to delete is
exactly the thing the audit must never lose.

So the fix is not "keep more archives". It is to take the merge records OUT
of the rotating journal and put them somewhere that does not rotate — the
same shape as the facts anchor (round 137: `.pair/facts.json` is deliberately
not in `state.json`, because "a fact that vanishes with a rotation is not an
anchor").

## What to do

- Move the merge trail to a non-rotating record. The candidates are already
  in the tree: `mcp/pair_mailbox/ledger.py` is a per-round ledger that is
  read by `round_summary.py` and the receipt layer, and it is not part of the
  transcript's rotation. A `pr_merged` / `pr_merge_repaired` / `pr_merge_forced`
  row written THERE survives the journal's window by construction.
- Make `scripts/merge-audit-trail.py` read that record instead of the
  transcript, and keep reading the transcript as the fallback for rooms
  whose ledger predates the change (so the audit does not go blind on an old
  room).
- Then the rotation window becomes a free parameter again: whatever the
  journal's size policy wants, the merge trail no longer rides on it.
- Measure before choosing a number: the merge rate over the surviving
  window is the input for "how many archives does the journal want", which is
  a separate question from the audit's.

## Red-proof

- After a merge, the merge record is readable by the audit **after** the
  journal has rotated past it. The test forces the rotation (the existing
  tests already do this by patching `TRANSCRIPT_MAX_BYTES`) and then asserts
  the audit still finds the merge.
- The audit still reports a merge with NO record anywhere as an escape — the
  fail-closed direction must survive the move.
- A room whose ledger has no merge rows but whose transcript does still
  audits correctly (the fallback path).

## Severity

medium — the audit fails loudly today rather than passing falsely, and the
repair command exists, so nothing is silently wrong. But the debt grows on
its own: every merge past the window is a manual repair, and a repair needs
a human to notice the audit went red.
