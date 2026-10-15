---
title: Манифест пишет commits [] для смерженного раунда (якорь master..HEAD пуст после merge)
found: 2026-10-15
severity: high
round: 95
status: open
---

## Где

`mcp/pair_mailbox/manifest.py:30-44` (`_git_commits` — `git log master..HEAD`), `mcp/pair_mailbox/manifest.py:300-301` (cross_check, commits gap).

## Что сломается

`.pair/rounds/94.json` пишет `commits: []` для раунда, смержившего PR #40 (771fd77). `_git_commits` диффит `master..HEAD` на той ветке, где стоят ноги в момент записи; писатель коммитит на локальном master и пушит в ветку раунда — после merge между master и HEAD ничего нет. Cross-check слеп той же слепотой: спрашивает тот же пустой вопрос у того же пустого ответа.

## Почему отложили

Найдено критиком opencode при ревью PR #41 (задача #112), вне скоупа задачи 111. Меняет подсчёт коммитов манифеста, не аудит бэклога.

## Что сделать

Привязать коммиты раунда к якорю, переживающему merge (тип ветки + head sha + merge sha в state при open/merge PR, либо `git log <anchor>..HEAD` от close-предыдущего раунда). Cross-check — против записанного якоря, не `master..HEAD`. Приёмка: re-derive раунда 94 называет merged sha; манифест [] при непустом якоре — отказ.
