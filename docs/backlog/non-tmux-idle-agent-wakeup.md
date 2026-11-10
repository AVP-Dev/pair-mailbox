---
title: Idle SSH agents cannot be awakened without a tmux pane
found: 2026-11-10
severity: medium
round: 154
status: open
---

## Where
`mcp/pair_mailbox/server.py:2143-2153`

## What will break
Messages reach the mailbox, but an idle CLI outside tmux is not resumed. Observed pair_send returned woke_peer=false and the peer did not consume the message. MCP itself still works.

## Why deferred
The current recovery fixes the stdio SSH/worktree launcher. This finding requires a separate change and acceptance checks; it is not claimed fixed.

## What to do
Provide a supported client API or managed PTY wake adapter with lifecycle tests; distinguish mailbox delivery from resumed processing.
