---
title: Wiring проверки свежести MCP-серверов в рантайме (server_freshness)
found: 2026-10-12
severity: high
round: 64
status: resolved
resolved_in: 78
---

## Где

`mcp/pair_mailbox/server.py` (вызовы `tool_pair_state`, `tool_pair_advance`), `mcp/pair_mailbox/server_freshness.py`, `bin/pairpilot`.

## Что это

В раунде 64 реализован изолированный модуль `server_freshness.py` с функциями проверки возраста запущенного процесса сервера относительно mtime файлов `mcp/pair_mailbox/*.py`.
Однако функции пока не вызываются в рантайме прод-сервера (`server.py`), из-за чего устаревший in-memory сервер не предупреждает о рассинхроне и не блокирует перевод фаз.

## Что сделать

1. Подключить `check_server_freshness` к `tool_pair_state`:
   - Добавить поля `server_fresh` (bool) и `server_stale_files` (list[str]) в ответ `pair_state`.
2. Подключить проверку в `tool_pair_advance`:
   - Если сервер устарел (`not server_fresh`), отклонять движение фазы вперед с `ProtocolError` и требованием перезапустить сервер (`bin/pairctl restart` / рестарт процесса).
3. Добавить детекцию и авторестарт/предупреждение в супервизоре `bin/pairpilot`.
4. Перезапустить in-memory процессы серверов после применения изменений и подтвердить свежесть в рантайме.

## Критерий приёмки

- `pair_state` возвращает `server_fresh: true` для свежего сервера и `false` со списком файлов при устаревании.
- `pair_advance` отказывает в движении вперед при устаревшем сервере.
- Тесты на связку (wiring) проходят.
- Монолит `server.py` и лимиты `file-size-baseline.tsv` соблюдены.

## Аудит (раунд 78)

- Пп. 1–2 закрыты в раунде 65 (`server.py:1794-1795`, `assert_server_fresh` в `tool_pair_advance`).
- П. 3 закрыт в раунде 65 (`Pilot.check_server_freshness`, `tick`, тест `test_pilot_detects_and_logs_stale_server`).
- Раунд 78 добавил: stale-check осознанно exempt из парковки done (docstring + тест `test_stale_server_warns_even_when_parked`); авторестарт не делается сознательно — MCP-серверы спавнит клиент, пилот только предупреждает.
- П. 4 (рестарт in-memory процессов) — процессный шаг при деплое изменений, кода не требует.
