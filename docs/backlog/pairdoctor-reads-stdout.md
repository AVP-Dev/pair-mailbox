---
title: `pairdoctor` читает `r.stdout`, а unittest пишет итог прогона в `stderr`
found: 2026-10-03
severity: low
---
## Где

`bin/pairdoctor:65-69`

## Что сломается

```python
tail = [l for l in r.stdout.splitlines() if l.startswith("Ran ") or l.strip() in ("OK", "FAILED")]
```

`unittest` пишет `Ran 79 tests in ...` и `OK` в `stderr`. Воспроизведено: в `stdout`
ноль вхождений `Ran `, в `stderr` — одно.

Поэтому `tail` всегда пуст, и успешная ветка всегда печатает голый `"OK"` без числа
тестов — то есть строка, ради которой её писали, не работает никогда. Ветка
провала полагается на `r.stderr[-200:]`, поэтому статус ок/ошибка не инвертируется:
именно поэтому severity и low.

## Почему отложили

Раунд был в режиме `audit` (`bin/pairctl mode audit`): работа раунда — evidence, а не diff, поэтому правка кода в нём не делалась. Находка подтверждена соседом и ждёт отдельного раунда `fix`.

## Что сделать

Читать `r.stderr`, а для счётчика тестов — искать `Ran ` в объединённом
`r.stdout + r.stderr`.
