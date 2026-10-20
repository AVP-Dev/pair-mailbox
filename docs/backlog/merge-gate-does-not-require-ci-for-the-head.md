---
title: The merge gate accepts a test evidence recorded for a different sha of the same round — a green promise that is not about the code being merged
found: 2026-10-20
severity: high
round: 130
status: open
---

# The merge gate's test evidence is bound to the round, not to the head

`bin/pairctl pr merge` refuses to merge without passing test evidence. The
evidence is looked up by round number only:

```
mcp/pair_mailbox/review_gate.py:43    cur_round = int(state.get("round", 0))
mcp/pair_mailbox/review_gate.py:44    round_ev = [ ... if int(ev.get("round", -1)) == cur_round ]
mcp/pair_mailbox/review_gate.py:48    has_test_ok = any(
mcp/pair_mailbox/review_gate.py:49        ev.get("kind") == "test" and ev.get("ok")
```

Nothing in that lookup compares the evidence with `head`. The head is read at
`review_gate.py:33` and used for the **verdict** (`review_gate.py:165`,
`review_gate.py:191`) and for the dirty-tree check — but the evidence passes
on a round number alone. The round number does not move when the head does,
so an evidence record written for one commit keeps vouching for every commit
after it in the same round.

**Live proof, this round.** PR #77's head was `b23e7c8`. The passing test
evidence in the state at that moment was recorded for `bf3914f` — two commits
behind, and itself a head whose CI had been red. The gate said allowed and
the merge went through (`6a451e0`). Nothing was wrong with the outcome — the
CI for `b23e7c8` was green and was checked by hand — but the gate did not
require that, and it could not have: it was reading an evidence record for
another commit.

**Why the difference matters more than it looks.** The verdict is a peer
reading the diff; the evidence is a machine saying the suite ran. A green
suite run is the only part of the gate that speaks about the code itself.
Round 130 measured how easily that part goes stale: the evidence recorded on
`2368a48` said "0 required failures, 0 warning(s), exit 0, 187 s" and that
was **true** — and CI on that same sha was **red on all five Python
versions**, because the test took the git author identity from the machine.
A record that says "the suite passed" is a record about a machine, not about
a commit, unless it names the commit.

**What to do.**

- The evidence needs the head it was measured on, and the gate needs to
  compare it. `session_facts` already writes the head into `.pair/facts.json`
  and `scripts/verify-ground-truth.py:24` already refuses a summary whose
  head differs from the facts — the same comparison, in the same shape, is
  what the merge gate is missing.
- Then the rule becomes: the evidence must be for the head being merged, and
  a new commit invalidates it, exactly as a new commit invalidates the
  verdict (`review.is_stale`).

**What it must not become.** The gate must not require CI itself — CI is a
GitHub matrix, and the local suite is the machine-checkable half. The honest
requirement is "the suite was run on this commit", and the honest way to
record that is the head, so the record can be compared.

**A related half, and it is a separate card.** A PR that cannot be built
into a merge ref reports **no checks at all**, which reads like "nothing to
report" instead of "nothing was checked" — see
`docs/backlog/squash-merge-breaks-the-round-branch.md`.
