---
title: `pair_send`/`pair_deliver` в несуществующую роль — REFUSED, а не мёртвый ящик
found: 2026-10-09
severity: high
round: 48
---

## Где

`tool_pair_send.apply` (`mcp/pair_mailbox/server.py:2345`); anchor при находке
был `server.py:2376` — проверка `known_target` шла **после** записи письма.

## Что это

Раунд 47, живой случай: 4 письма ушли в роль `opencode-agent`, которой нет в
ростере. Сервер создал ей мёртвый ящик (`.pair/mailbox/opencode-agent/`),
отчитался `delivered: true` и `target_is_registered: false`, и сообщения
пропали молча. Пилот 20 раз эскалировал пробуждение фантома
(`.pair/pilot/opencode-agent.txt`, `.pair/wake/opencode-agent.json`).

## Что сделано (250819b)

`apply` теперь отказывает с `ProtocolError` раньше записи: роль не в ростере —
не «ещё не стартовала», а опечатка или фантом. Текст отказа перечисляет живые
роли. Ящик не создаётся.

## Остаток / критерий приёмки

- Тест на отказ и отсутствие каталога добавлен в
  `test_protocol.py` рядом с `test_cannot_send_to_yourself_or_nowhere`.
- `pair_deliver` наследует отказ через внутренний вызов `tool_pair_send`.
