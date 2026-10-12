---
title: Канал панели: явный `--pane`/`PAIR_PANE` от запускающей стороны вместо наследованного env
found: 2026-10-09
severity: high
round: 48
status: resolved
resolved_in_round: 63
---

## Где

`_register` (`mcp/pair_mailbox/server.py:1240-1280`), `extract_pane` (`mcp/pair_mailbox/pairctl_read.py:165-185`),
занятость (`mcp/pair_mailbox/server.py:1393-1406`), тесты `mcp/pair_mailbox/test_protocol.py:2430-2465`
и `mcp/pair_mailbox/test_pairctl_read.py:391-407`.

## Что это

MCP-демон OpenCode порождает общий orphan-сервис `opencode serve --service`
(ppid=1), env которого несёт `TMUX_PANE` той панели, где сервис родился
(сегодня — `gala:0.1`, ДРУГОЙ проект). Env-маршрут честно режет чужим проектом
(`pane_env_rejected` с 04:39, 9+ раз), ancestry пуст (цепь демона не содержит
`pane_pid` — TUI и демон разные деревья), итог `pane=None` + mailbox-only молча.
Вечером: мёртвый `agy` держал живую `mail:0.1` в дедлоке «pid мёртв И панели нет».

Failing-before: демон с чужим `TMUX_PANE` остаётся без адреса (воспроизведено
вживую 2026-10-09).

## Что сделать

Явный канал `--pane`/`PAIR_PANE` от запускающей стороны (pairup/pair-duo сидят
в своей панели и знают её; проверить, отдаёт ли local-MCP opencode environment).
Приоритет источников: pin > явный канал > env > ancestry. В занятости: записанная
панель жива + проектна + держатель мёртв + панель занята живым процессом → снять
запись с транскриптом, а не дедлокить.

## Критерий приёмки

- Тест на демон-симулятор с чужим env: адрес приходит из явного канала.
- Тест на дедлок: живой хозяин получает панель мёртвого держателя.
