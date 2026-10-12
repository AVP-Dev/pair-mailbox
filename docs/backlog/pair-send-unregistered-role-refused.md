---
title: `pair_send`/`pair_deliver` в несуществующую роль — REFUSED, а не мёртвый ящик
found: 2026-10-09
severity: high
round: 48
resolved_in_round: 62
status: resolved
---

## Где

`tool_pair_send.apply` (`mcp/pair_mailbox/server.py:2357-2369`),
`tool_pair_deliver` (`mcp/pair_mailbox/server.py:2254`).

## Что это

Раунд 47, живой случай: 4 письма ушли в роль `opencode-agent`, которой нет в
ростере. Сервер создал ей мёртвый ящик (`.pair/mailbox/opencode-agent/`),
отчитался `delivered: true` и `target_is_registered: false`, и сообщения
пропали молча. Пилот 20 раз эскалировал пробуждение фантома
(`.pair/pilot/opencode-agent.txt`, `.pair/wake/opencode-agent.json`).

## Что сделано (250819b, раунд 48 и раунд 62)

1. `apply` отказывает с `ProtocolError` раньше записи: роль не в ростере —
   не «ещё не стартовала», а опечатка или фантом. Текст отказа перечисляет живые
   роли. Ящик не создаётся.
2. `pair_deliver` наследует отказ через внутренний вызов `tool_pair_send`.
3. В `test_protocol.py:1889-1907` (`test_cannot_send_to_yourself_or_nowhere`)
   добавлены явные тесты на отказ `tool_pair_send` и `tool_pair_deliver` на `ghost-role`
   и отсутствие каталога в `mailbox_root`.
