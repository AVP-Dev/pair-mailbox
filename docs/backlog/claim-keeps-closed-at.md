---
title: `pair_claim` не снимает `closed_at`: живой писатель числится в закрытом раунде
found: 2026-10-03
severity: high
---
## Где

`mcp/pair_mailbox/server.py:417-446` (все ветки `tool_pair_claim`)
`mcp/pair_mailbox/server.py:700-709` (`pair_advance` при переоткрытии — чистит)
`mcp/pair_mailbox/server.py:363-374` (`_peer_status`)

## Что сломается

Ни одна ветка `pair_claim` не трогает `closed_at`.

Воспроизведено: раунд закрыт (`phase=done`, `closed_at` есть) → `pair_release` →
сосед вызывает `pair_claim`. Итог: `phase=done`, `closed_at` на месте, `round`
1→2, писатель живой. Следствия:

- `_peer_status` (server.py:363-374) сообщает другому агенту
  `will_read_new_messages: false` и «This role has moved on and will not read new
  messages» — про писателя, который только что взял авторитет;
- `project_row` (server.py:1575) считает проект закрытым, поэтому `pairctl all`
  не показывает его работающим и никогда не пометит зависшим (server.py:1589).

Восстановление есть (`pair_advance("implement")` чистит `closed_at`), но новому
писателю никто не говорит, что это нужно, а `pair_state` показывает
`phase: done, round: 2` — противоречие без объяснения.

## Почему отложили

Раунд был в режиме `audit` (`bin/pairctl mode audit`): работа раунда — evidence, а не diff, поэтому правка кода в нём не делалась. Находка подтверждена соседом и ждёт отдельного раунда `fix`.

## Что сделать

1. `pair_claim`, ведущий авторитет на закрытом раунде, обязан снимать `closed_at`
   и возвращать фазу в `implement` — либо отказываться.
2. `_peer_status` должен смотреть на живость роли, а не только на `phase`, если
   `round` вырос после закрытия.
3. Тест: claim на закрытом раунде → `closed_at` нет, сосед видит
   `will_read_new_messages: true`.
