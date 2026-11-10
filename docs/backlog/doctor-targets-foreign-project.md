---
title: MCP doctor identifies panes and processes without project identity
found: 2026-11-10
severity: high
round: 154
status: open
---

## Where
`bin/pairmcpdoctor:78-92`

## What will break
Global role-name matching can use another project server as evidence of health or send recovery keys into another project pane. Independently confirmed against the source.

## Why deferred
The current recovery fixes the stdio SSH/worktree launcher. This finding requires a separate change and acceptance checks; it is not claimed fixed.

## What to do
Scope pane and process selection to the resolved project room and tmux socket; exercise two concurrent projects.
