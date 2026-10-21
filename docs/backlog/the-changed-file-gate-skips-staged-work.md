---
title: The changed-file gate skips everything that is already staged
found: 2026-10-21
severity: medium
round: 139
status: open
---

## Where

- `scripts/verify-changed.sh` — the file discovery
- `scripts/verify-map.tsv` — the map, and its own test (`test_verify_map.py`)

## What breaks

The gate discovers the changed files with `git diff --name-only` (the working
tree against the **index**) plus `git ls-files --others` (the untracked). A
change that is already staged is in neither list: `git diff` compares the
worktree to the index, and a staged file is tracked. So `git add` before
running the gate makes it print

```
verify-changed: nothing to check, the tree is clean
```

and exit 0 — a green gate that ran nothing. The usual order (edit → gate →
commit) hides it, but staging first is a common habit, and round 138 ran the
gate after the commits and got exactly that answer, and read it as "the tree
is clean" rather than "the gate saw nothing".

The map's own test closes the other half of the door: a file named in
`scripts/verify-map.tsv` must be tracked (`git ls-files`), so the gate cannot
see a new file until it is staged — and once it is staged, the gate cannot
see it either. There is no state in which the per-commit gate covers a new
file the map names.

## Acceptance

- The discovery includes the staged changes (`git diff --cached --name-only`),
  so a staged change selects its suite.
- A test: a staged change is selected, and a new file named in the map is
  covered by its suite.
- The branch-level run (`./scripts/verify-changed.sh --base origin/master`)
  covers the files of a whole branch, including the new ones.
