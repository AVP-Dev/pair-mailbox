---
title: `pair_state` показывает недозволенные ходы тому, кому их делать нельзя
found: 2026-10-03
severity: medium
---
## Где

`mcp/pair_mailbox/server.py:334` (`legal_next_phases`)
`mcp/pair_mailbox/server.py:71-77` (`PHASE_TRANSITIONS`) и `:678-683` (отказ)

## Что сломается

`legal_next_phases` — это `PHASE_TRANSITIONS[phase]`, то есть ходы **вперёд для
писателя**, независимо от того, кто спрашивает.

Воспроизведено: писатель `agy`, фаза `plan`, спрашивает `opencode` (не писатель) →
`pair_state` отдаёт `legal_next_phases: ['scoping', 'implement']`, а
`pair_advance("implement")` **отказан**: `only the writer moves the phase forward`.
При этом `hint` внутри отказа предлагает другой список.

Состояние предлагает недозволенный ход именно тому, кому его делать нельзя.
§10a запрещает инструкции, противоречащие состоянию, — здесь состояние
противоречит серверу.

## Почему отложили

Раунд был в режиме `audit` (`bin/pairctl mode audit`): работа раунда — evidence, а не diff, поэтому правка кода в нём не делалась. Находка подтверждена соседом и ждёт отдельного раунда `fix`.

## Что сделать

`legal_next_phases` должен считаться от роли: писателю — вперёд, остальным — только
`PHASE_BACKWARD[phase]`. Один расчёт, один источник правды.
