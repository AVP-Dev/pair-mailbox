---
title: Socket client blocks on JSON-RPC notifications
found: 2026-11-10
severity: high
round: 154
status: open
---

## Where
`mcp/pair_mailbox/mcp_socket_client.py:137-144`

## What will break
After notifications/initialized, pump waits for a response that the server never sends. The subsequent tools/list cannot be forwarded. Reproduced with real sockets and independently confirmed by the reviewer.

## Why deferred
The current recovery fixes the stdio SSH/worktree launcher. This finding requires a separate change and acceptance checks; it is not claimed fixed.

## What to do
Forward both directions independently and test initialize, notification, tools/list and asynchronous notifications.
