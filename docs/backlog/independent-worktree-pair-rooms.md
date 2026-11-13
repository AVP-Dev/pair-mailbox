---
title: Independent pairs in one repository need separate rooms
found: 2026-11-10
severity: medium
round: 154
status: open
---

## Where
`bin/pairup:159-161; mcp/pair_mailbox/store_core.py:367-387`

## What will break
Worktrees share the main project room by default. Two independent codex pairs then contend for one role lock. Different working directories do not provide independent protocol state. This is a missing supported mode, not a reason to remove singleton locking.

## Why deferred
The current recovery fixes the stdio SSH/worktree launcher. This finding requires a separate change and acceptance checks; it is not claimed fixed.

## What to do
Design explicit per-run rooms and socket identity, including MCP configuration routing, while preserving one writer per room and compatibility with the shared-room default.
