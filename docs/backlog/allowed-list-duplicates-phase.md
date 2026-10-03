---
title: в отказе фаза печатается дважды: «allowed: implement, implement»
found: 2026-10-03
severity: low
---
## Где

`mcp/pair_mailbox/server.py:686-687`

## Что сломается

```python
allowed = list(forward) + ([backward] if backward else [])
```

В фазе `done` вперёд (`PHASE_TRANSITIONS["done"]`) и назад
(`PHASE_BACKWARD["done"]`) — одна и та же фаза, и она попадает в список дважды.

Воспроизведено дословно:
- `cannot move from 'done' to 'done'; allowed: implement, implement.`
- `cannot move from 'done' to 'scoping'; allowed: implement, implement.`

## Почему отложили

Раунд был в режиме `audit` (`bin/pairctl mode audit`): работа раунда — evidence, а не diff, поэтому правка кода в нём не делалась. Находка подтверждена соседом и ждёт отдельного раунда `fix`.

## Что сделать

Дедуплицировать список при сборке, сохраняя порядок.
