---
name: pair-backlog
description: Park a real finding that will not be fixed in this run, without losing it. Use when a review finds something genuinely broken but out of scope, or when the user says "в бэклог", "отложим", "не сейчас", "park it", "defer this".
---

# Backlog

> Language: this is the English original. The Russian mirror is [SKILL.ru.md](./SKILL.ru.md).

A finding is real but outside this task's scope. The backlog holds what will break later.
Do not invent trivia.

## How

1. `pair_note(kind="backlog", text=...)`. Format: `file:line — what will break — why deferred`.
2. At the end of the task the writer moves findings into `docs/backlog/<slug>.md`, one file each:

```markdown
---
title: <brief>
found: <date>
severity: high|medium|low
round: <N>
status: open
---
## Where
`file:line`
## What will break
<scenario>
## Why deferred
<reason>
## What to do
<step>
```

Lifecycle: `status: open` while the debt lives. A fixed card is DELETED from
`docs/backlog/` (the commit message records what closed it) — history keeps
the record, the directory keeps only open debt. The close report lists what
was parked and what stays open, so nothing dies silently.

## Rules

- Only the writer creates files in `docs/backlog/`. The critic puts the finding in a note.
- What takes a minute to fix — fix now. The backlog grows from laziness.

## In `mode=audit`

In an `audit` round this skill is the main outlet for findings, not a fallback.
No changes are made at all, so "deferred" here is not the exception but the whole
outcome: anything confirmed by sources goes into `docs/backlog/<slug>.md`.

The difference is one, and it matters: in `fix` this holds what did not fit the
task's scope. In `audit` it holds **everything confirmed**, and "but this takes
a minute to fix" is not a reason to skip. There is one reason only: the fix
round was not ordered, and a different one will be started later. A finding
without a file will not be found in six months.
