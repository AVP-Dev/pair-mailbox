---
title: Project tmux socket is passed as part of the executable name
found: 2026-11-10
severity: high
round: 154
status: open
---

## Where
`mcp/pair_mailbox/composer.py:128-147,288-303,1416; mcp/pair_mailbox/server.py:719-737,2166`

## What will break
When `PAIR_PANE_SOCKET` names a project tmux server, `_tmux()` and `tmux_command()` return a single string containing both the tmux executable and `-L <socket>`. Pane helpers pass that string as the first element of a `subprocess.run([...])` argument list, so the OS searches for a binary whose name includes the socket flags and raises `FileNotFoundError`. Pane identity checks and pointer fallback delivery then fail; the message remains in the mailbox without reaching the neighbour's pane.

## Why deferred
Task #158 fixes the doctor/server role-lock collision. Correcting tmux command construction crosses the shared composer and server call sites and needs its own argv-routing tests.

## What to do
Have the tmux resolver return an argument vector and pass it unchanged to every subprocess call. Add coverage with `PAIR_PANE_SOCKET` set for pane identity and delivery, including the pointer fallback path.
