---
title: Wiring проверки свежести MCP-серверов в рантайме (server_freshness)
found: 2026-10-12
severity: high
round: 64
status: open
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
