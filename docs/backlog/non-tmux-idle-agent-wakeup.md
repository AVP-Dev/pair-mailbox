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

## Measured update (2026-11-10)

- `bin/pair-runner` launches `opencode run --standalone --auto` as a finite
  subprocess and promises mailbox delivery only. It does not own a persistent
  interactive session or a control socket (`bin/pair-runner:2-4,17-18,168-176`).
- `pairpilot` returns before trying any API adapter when it has no tmux instance
  or no pane (`bin/pairpilot:872`). Its only shipped API adapter is
  OpenCode (`bin/pairpilot:837-838`); the other CLI roles have no adapter
  (`docs/PROTOCOL.md:414-431`).
- Current Codex CLI documentation describes `thread/resume` / `turn/start` over
  the app-server protocol and `codex exec resume <thread-id>`. Codex MCP tool
  calls carry `threadId` metadata. However, this MCP server is a child process
  on stdio and receives no app-server endpoint or client handle that could
  submit a turn to the already-running TUI. Starting a separate `codex exec
  resume` process is not a proven wake of that TUI and could run a competing
  executor against the same thread. Do not report it as waking the existing
  agent without a lifecycle/ownership contract.
- Safe implementation needs a managed client/session endpoint (or managed PTY
  owner) that can accept a wake request and report whether the active agent
  resumed. Until then, mailbox delivery must remain separate from resume
  confirmation.
