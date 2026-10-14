---
title: Команда перезапуска пары — bin/pairrestart
found: 2026-10-14
severity: high
round: 92
status: open
---

## Где

`mcp/pair_mailbox/serve.sh` (relaunch-обёртка, не рестарт),
`mcp/pair_mailbox/server.py:919` (LOCK_EX|LOCK_NB — отказ второму процессу),
предложение `.pair/notes/finding-round91-no-restart-procedure.md:88-128`,
черновик `.pair/notes/finding-round91-pairrestart-draft.md`.

## Что это

Команды restart нет: в `bin/` только pair-duo/pairboard/pairchat/pairctl/
pairdoctor/pairpilot/pairup/pairwatch. `serve.sh` запускает один сервер роли.
Наивный рестарт умирает actionless-traceback (см. карту pairrestart-hint).
Человек 2026-10-14: «не понимаю где взять пид — нужна одна команда».

## Что сделать

`bin/pairrestart` по черновику: diagnose (pid/start/старшинство кода на роль)
→ план словами с ценой → один вопрос yes → kill устаревшего → relaunch пилота
→ verify (pairdoctor + pairctl status) + честный отчёт о непереподключённых
ролях. Сервер роли — только kill; relaunch — переподключением сессии
(`/mcp`). Без force-push/merge (§12 не трогать).
