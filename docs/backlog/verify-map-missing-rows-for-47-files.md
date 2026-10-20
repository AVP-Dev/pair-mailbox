---
title: 47 tracked files have no row in verify-map.tsv — a change to any of them runs the whole positive layer
found: 2026-10-20T08:05Z
severity: low
round: 129
status: open
---

## Where

- `scripts/verify-map.tsv` — the file-to-suite table
- `scripts/verify-changed.sh` — the `UNKNOWN` fallback that runs when a path has no row

## What breaks

After `docs/backlog/verify-changed-prefix-rows-never-matched.md` repaired the
selector, the map's directory rows work and 87 previously-dead files reach
their suite. What the repair exposed is the remaining gap: **47 tracked files
have no row at all**, so the selector returns `UNKNOWN` for them and
`verify-changed.sh` falls back to running the ENTIRE positive layer.

The fallback is honest — the gate says "no mapping, running the whole positive"
rather than pretending — but it is slow: the whole positive layer is minutes,
where a single suite is seconds. A developer editing `mcp/pair_mailbox/plan_gate.py`
pays the full layer for a change that one suite would cover.

The list (measured 2026-10-20 on the round-129 tree, 45 files):

```
bin/pair-duo  bin/pairctl  bin/pairmcpdoctor  bin/pairmcprestart
bin/pairrelaunch  bin/pairwatch  finish.sh
mcp/pair_mailbox/board_hosts.py  mcp/pair_mailbox/board_render.py
mcp/pair_mailbox/board_snapshot.py  mcp/pair_mailbox/lock_reaper.py
mcp/pair_mailbox/mcp_socket_client.py
mcp/pair_mailbox/pair_notes_consistency.py
mcp/pair_mailbox/pair_test_base.py  mcp/pair_mailbox/pairctl_cli.py
mcp/pair_mailbox/plan_gate.py  mcp/pair_mailbox/read_wait.py
mcp/pair_mailbox/state_resolve.sh  mcp/pair_mailbox/store_core.py
mcp/pair_mailbox/task_banner.py  mcp/pair_mailbox/task_subject.py
mcp/pair_mailbox/test_branch_audit.py  mcp/pair_mailbox/test_plan_gate.py
mcp/pair_mailbox/test_task_banner.py  mcp/pair_mailbox/test_worktree.py
mcp/pair_mailbox/worktree.py  mcp/pair_mailbox/assets/board.js
mcp/pair_mailbox/assets/labels.js  mcp/pair_mailbox/assets/board.css
mcp/pair_mailbox/serve.sh  mcp/pair_mailbox/SLICE_C_SOCKETS.md
.opencode/agents/verifier.md  .opencode/commands/pair-handoff.md
.opencode/commands/pair-status.md  AGENTS.md  CONTRIBUTING.md
CODE_OF_CONDUCT.md  SECURITY.md  LICENSE  .gitignore
.github/workflows/ci.yml  .github/pull_request_template.md
.github/ISSUE_TEMPLATE/bug_report.md
.github/ISSUE_TEMPLATE/feature_request.md
.claude/settings.json  assets/pair-flow-diagram.svg
assets/pair-mailbox-banner.svg
```

(A further 2 files — `install_test.py` and `scripts/verify-map.tsv` — were in
this set and got exact rows in round 129, because the round touched both.)

## What to do

Give each file the row its own suite belongs to. Most of them have one:

- `mcp/pair_mailbox/plan_gate.py` → `plan-gate`
- `mcp/pair_mailbox/read_wait.py` → `read-wait`
- `mcp/pair_mailbox/pair_notes_consistency.py` → `notes-consistency`
- `mcp/pair_mailbox/store_core.py` → `ledger`
- `mcp/pair_mailbox/pairctl_cli.py` → `pairctl-panes`
- `mcp/pair_mailbox/test_plan_gate.py` → `plan-gate`
- `mcp/pair_mailbox/test_task_banner.py` → the suite that owns `task_banner.py`
- `bin/pair-duo` → `pair-duo`
- `bin/pairctl` → the pairctl suite

The markdown and config files (AGENTS.md, the `.opencode/` commands, the
GitHub templates) have no suite of their own; they are covered by the
`waiting-contract` and `branch-class` checks, so their rows should name those
suites — or a `docs-check` row, which says "covered by static".

The map's own guard (`mcp/pair_mailbox/test_verify_map.py`) checks that every
path the map names EXISTS and every suite it names is in the profile. It does
not check the other direction: that every source file HAS a row. That check is
the guard that would stop this gap from growing again — add it, and it will
name each file the moment it is added without a row.

## Red-proof

Add a source file without a map row and run
`bash scripts/verify-changed.sh --suite-for <the new file>`: it prints
`UNKNOWN`. With the guard added, the suite that owns the map fails and names
the file.

## Severity

low — nothing is broken; the fallback is correct and says so. The cost is
time in the changed-file gate, on the files a contributor is most likely to
touch.
