---
name: pair-backlog
description: Park a real finding that will not be fixed in this run, without losing it. Use when a review finds something genuinely broken but out of scope, or when the user says "в бэклог", "отложим", "не сейчас", "park it", "defer this".
---

# Бэклог

Находка настоящая, но не в объёме этой задачи. В бэклог идёт то, что сломается позже.
Не выдумывай мелочей.

## Как

1. `pair_note(kind="backlog", text=...)`. Формат: `file:line — что сломается — почему отложили`.
2. Писатель в конце задачи переносит находки в `docs/backlog/<слаг>.md`, по одному файлу:

```markdown
---
title: <коротко>
found: <дата>
severity: high|medium|low
---
## Где
`file:line`
## Что сломается
<сценарий>
## Почему отложили
<причина>
## Что сделать
<шаг>
```

## Правила

- Только писатель создаёт файлы в `docs/backlog/`. Критик кладёт находку в заметку.
- Что правится за минуту — чинить сразу. Бэклог растёт от лени.
