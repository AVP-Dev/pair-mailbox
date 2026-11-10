---
title: MCP doctor holds the lock needed by the replacement server
found: 2026-11-10
severity: high
round: 154
status: open
---

## Where
`bin/pairmcpdoctor:95-105,197-220; mcp/pair_mailbox/store_core.py:384-387`

## What will break
The doctor holds .server-ROLE.lock during reconnect. The replacement server needs the same exclusive lock and immediately refuses. Confirmed by both reviewers against the code.

## Why deferred
The current recovery fixes the stdio SSH/worktree launcher. This finding requires a separate change and acceptance checks; it is not claimed fixed.

## What to do
Use a separate doctor coordination lock and prove replacement-server startup during recovery.
