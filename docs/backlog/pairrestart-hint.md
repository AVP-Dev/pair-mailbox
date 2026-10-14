---
title: Отказ замка роли не печатает remedy (hint теряется на старте)
found: 2026-10-14
severity: medium
round: 92
status: open
---

## Где

`mcp/pair_mailbox/server.py:632-638` (ProtocolError.__init__: hint отдельно),
`server.py:923-928` (remedy «kill the double and restart» в args[1]),
`server.py:4175` (main зовёт acquire_role_lock без try),
`server.py:4025` (печать hint — только invoke-путь).

## Что это

Второй процесс на занятой роли умирает с `ProtocolError: role ... already
held`, а единственное операционное знание (сначала kill, потом relaunch)
сидит в hint и до терминала не доходит. Проверено чтением: стартовый путь
без try, печать hint — только на invoke.

## Что сделать

Обернуть acquire_role_lock в main() в try/ProtocolError и печатать
`message + hint` в stderr с exit != 0. Поведенческий тест: занять замок,
звать main, ждать remedy-строку в stderr.
