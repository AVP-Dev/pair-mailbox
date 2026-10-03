---
title: `mode=audit` закрывает только резервирование патча, но не seal и verify
found: 2026-10-03
severity: medium
---
## Где

`mcp/pair_mailbox/server.py:726` — единственный вызов `_require_fix_mode`
`mcp/pair_mailbox/server.py:773` (`pair_patch_seal`) и `:802` (`pair_patch_verify`)

## Что сломается

`_require_fix_mode` зовётся ровно в одном месте — `tool_pair_patch_reserve`.
`pair_patch_seal` и `pair_patch_verify` её не зовут.

Воспроизведено: патч зарезервирован при `mode=fix`, человек переключает раунд на
`audit`, после чего `seal` **проходит** (sha256 посчитан) и `verify` **проходит**
(`match: True`, `verdict: "Hash matches... apply"`).

То есть раунд, объявленный «без diff», выдаёт писателю зелёный вердикт на
применение. §10a обещает, что в аудите патчей нет; на деле нет только одного шага
из трёх. Плюс `pair_patch_release` — единственный шаг, который в аудите работает
как надо, потому что чистить мусор надо и там.

## Почему отложили

Раунд был в режиме `audit` (`bin/pairctl mode audit`): работа раунда — evidence, а не diff, поэтому правка кода в нём не делалась. Находка подтверждена соседом и ждёт отдельного раунда `fix`.

## Что сделать

1. `_require_fix_mode` в `pair_patch_seal` и `pair_patch_verify`. `pair_patch_release`
   оставить работающим: освобождение мусора в аудите нужно.
2. Тест на все три шага, а не только на reserve — сейчас есть только reserve
   (`test_audit_refuses_a_patch_and_records_the_refusal`).
