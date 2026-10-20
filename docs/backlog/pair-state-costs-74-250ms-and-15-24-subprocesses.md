---
title: pair_state costs 74-250 ms and 15-24 subprocesses per call, with 5 duplicate `git rev-parse HEAD` and 4 identical tmux queries
found: 2026-10-19T23:34Z
severity: medium
round: 126
status: open
---

## Where

`mcp/pair_mailbox/server.py:1255` (`tool_pair_state`) and its helpers:
`_register` (`:789`), `_git_fingerprint` (`:746`), `_done_blockers` (`:1494`),
`store_core.py:343` (`_save`), `review.py:41/153/165/378/422/429`,
`composer.py:276` (`pane_identity`), `pair_notes_consistency.py:99`
(`check_notes`).

## What breaks — measured, on the live room

Own measurement script against `PairSession('omp', Store('.pair'))`:

```
pair_state (cache disabled) ms 166.1
pair_state (cache hit)       ms 74.0    subprocesses per call 15
pair_state (first, cold)     ms 249.7
```

Subprocesses in ONE uncached `pair_state`, captured by wrapping
`subprocess.run`:

```
2 x git rev-parse HEAD
1 x git status --porcelain
1 x git show --pretty=format: --name-only 08fbaec…
1 x git diff --no-ext-diff HEAD --
1 x git remote
1 x git for-each-ref --format=%(refname) refs/remotes/
1 x git merge-base --is-ancestor 08fbaec… refs/remotes/origin/HEAD
3 x tmux list-panes -a -F '#{pane_id} #{pane_pid}'
12 x tmux display-message -p -t %0 …      (four identical queries per call)
```

### The duplicates, with their call sites

`git rev-parse HEAD` — 5 times per call, from four different places:

- `review.snapshot` → `review.git_head` (`review.py:429` → `review.py:41`)
- `review.delivery_floor` → `review.git_head` (`review.py:165` → `review.py:41`)
- `review.origin_truth` → three `git` calls of its own (`review.py:398`,
  `:401`, `:403`), reached from `server.py:1509` (`origin_truth` itself is at
  `review.py:378`)

`tmux display-message -p -t %0` — 4 times per call, with the **same argument
string** every time, from:

- `server.py:929` → `composer.pane_belongs_to_project`
- `server.py:998` → `same_pane` (twice)
- `server.py:1064` → `composer.session_address`
- `server.py:1154` and `:1171` → `composer.pane_identity`

`check_notes` — 79.7 ms, called on EVERY `pair_state` (through
`_done_blockers` → `server.py:1521`), although it is a close-time check: it
re-reads every note file and the whole transcript and compares them.

Component costs:

```
review.snapshot      12.7 ms
review.delivery_floor 9.0 ms
check_notes          79.7 ms
Store._save (137 KB)  5.71 ms
Store.read            1.43 ms
```

## Why it matters

`statecache.py:3` states the usage this serves: "a full `pair_state` answer
costs ~939 tokens, called 3-4 times per round by agents". The cache shortens
the ANSWER, but not the WORK — the fingerprint itself (`statecache.fingerprint`,
`statecache.py:88`) calls `_git_fingerprint`, which is where the git calls come
from. So a cached call still costs 74 ms and 15 processes.

Under `auto` autonomy with a supervisor ticking in the background, the pair
spends a measurable part of its budget re-asking git for the same HEAD and tmux
for the same pane. On a loaded machine those subprocesses are the slowest part
of the answer.

## What to do

- Compute `git_head` once per call and pass it down; `review.snapshot`,
  `review.delivery_floor` and `review.origin_truth` all want the same SHA.
- Compute `pane_identity` once per call in `_register` and reuse the result for
  `pane_belongs_to_project`, `same_pane` and `session_address` — the same pane
  is being described four times.
- Move `check_notes` out of `_done_blockers` into the close path: it is a
  close-time check and does not belong in every state read.
- Do not rewrite the whole `state.json` when only `roles[…].last_seen`
  changed; the 137 KB write (`_save`, 5.71 ms) is paid on every call.

## Red-proof

Count the subprocesses a single `pair_state` spawns, before and after. Before:
24 uncached / 15 cached. After: the same work should take one `git rev-parse
HEAD` and one `tmux display-message` per pane.

## Severity

medium — not a defect but the single largest avoidable cost in the protocol's
hottest path, and the audit brief asks for optimisations explicitly.
