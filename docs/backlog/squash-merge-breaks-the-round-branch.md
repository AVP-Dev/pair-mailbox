---
title: A squash merge drops the round's branch from master's history — the next commit on that branch conflicts, and its CI never runs
found: 2026-10-20
severity: medium
round: 130
status: open
---

# After a squash merge, the round's branch is not an ancestor of master

`bin/pairctl pr merge` merges with squash, and that is deliberate: the merge
command is hardcoded at `mcp/pair_mailbox/review_gate.py:203` —
`cmd.extend(["--squash", "--delete-branch"])`. A squash merge produces one
commit with a single parent, so the branch's own commits are **not** in
master's history.

Measured on round 130: PR #75 merged as `259116c` with one parent, `b3babb0`.
The branch head `bf3914f` is not an ancestor of `origin/master` —
`git merge-base --is-ancestor bf3914f origin/master` exits non-zero.

**What breaks.** The next commit on that branch has a merge base that slid
back to the common ancestor `b3babb0`. A file added on the branch and also
present on master then lands in an **add/add conflict** — that is exactly
what `docs/rounds/130.md` hit. The PR is `CONFLICTING` / `DIRTY`.

**The trap is silent, and it is the dangerous half.** GitHub cannot build a
merge ref for a conflicting PR, so **no CI run exists at all**:
`gh pr checks 76` answers "no checks reported on the 'round-130-backlog-gate'
branch", and `statusCheckRollup` is empty. A reader who asks "is it green?"
gets an empty list, which reads as "nothing to report" and not as "nothing
was checked". The failing case looks like the passing case. The same emptiness
is what a branch with no workflow file would produce, and the same emptiness
a just-created PR produces before the runner picks it up.

Round 130 lost the branch to this: PR #76 was unmergeable, had zero checks,
and was closed; the change was re-based onto `origin/master` as
`round-130-card-ci-truth` (PR #77, head `b23e7c8`) and merged as `6a451e0`.

**What to do.** The rule has to live where the next agent will read it,
before the next branch is created, not in the card of the round that
discovered it:

- The protocol's merge section must say that a squash merge removes the round
  branch from master's history, so the next branch is created from a fresh
  `origin/master` — never from the previous round's branch.
- The writer's checklist must treat "branch created from the previous round's
  branch" as an error, the same way it treats a direct `gh pr merge` as an
  error (`docs/PROTOCOL.md:783`).

**Two directions, and the choice is not obvious.** The rule above works with
the current squash behaviour. The other direction is to stop squashing
(`--merge` instead), which keeps the branch as an ancestor and makes
follow-up commits merge cleanly — at the price of a merge commit per PR and a
different history shape. The repo's own merge path deliberately chose squash,
so the rule is the cheaper fix, but the decision belongs to a round that
looks at the whole history shape, not to this card.

**The other half is a separate defect.** A PR with no checks is not a green
PR, and nothing in the merge path says so: the gate binds the **verdict** to
HEAD (`review_gate.py:165`, `review_gate.py:191`) but does not require that
CI ran at all. That lives in
`docs/backlog/merge-gate-does-not-require-ci-for-the-head.md`.
