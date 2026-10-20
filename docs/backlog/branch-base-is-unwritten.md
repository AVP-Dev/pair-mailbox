---
title: Nobody knows which branch a round targets — `origin/dev` is undocumented and stale
found: 2026-10-18
severity: medium
round: 115
status: open
---

# Nobody knows which branch a round targets (`origin/dev` is undocumented and stale)

Found by the human's question of 2026-10-18 ("куда делать ветку дев? и почему не удалены
ненужные временные от раундов бранчи?"), after round 115 closed.

Measured 2026-10-18: `origin/dev` head is `3eb5d87` (round 113); `origin/master` head is
`80fe822` (round 115). dev carries 2 commits of its own and 0 that master lacks; master
carries 5 that dev lacks. So dev is not a downstream queue and not a fork — it is a branch
stopped two rounds ago.

`grep` for `dev` across `docs/` returns **no matches**. No protocol text, no backlog card,
no README sentence names a branch policy. `PROTOCOL.md` governs *how* to merge and is
silent on *into what*. So the repo's own contract leaves the base to whoever happens to
run `gh pr create`, which is how a live `dev` branch rots without anyone deciding anything.

Also still true: `--delete-branch=false` was passed deliberately (the sanctioned path in
`review_gate.py:15` merges with `--delete-branch`, i.e. the head branch of a round is
expected to go away after merge), so `origin/round-114-pilot-push-gates` and
`origin/round-115-store-core-and-loud-root` remain on the remote, both already merged.

Human decision owed, not an agent decision:
- **Option A** — dev is a forgotten artifact: delete it (and the two merged round branches
  with it), and remove any promise of a `dev` flow from the docs. Rounds target `master`.
- **Option B** — dev is the integration branch: rounds target `dev`, and `master` only ever
  fast-forwards from `dev`. This needs `docs/PROTOCOL.md` to say so, in the same section
  that names the sanctioned merge path, so the next round does not choose silently.

Until the human picks, do not invent a policy and do not delete anything: the previous
deletion-related mistake this round was doing something first and being asked afterwards.

The merge that accompanied this question ran outside the sanctioned path and left a hole
in the audit trail — that is a separate defect, and it lives in
`docs/backlog/merge-escaped-the-audit-trail.md`.
