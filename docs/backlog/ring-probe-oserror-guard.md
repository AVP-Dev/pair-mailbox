---
title: `_ring` probe без guard на OSError ронял звонящего вместе с доставкой
found: 2026-10-09
severity: medium
round: 48
resolved_in_round: 48
status: resolved
---

## Где

`_ring` (`mcp/pair_mailbox/server.py:2077-2080`); guard по образцу уже
существовал в той же функции на `server.py:2091-2098` (панель исчезла между
проверками) — у probe его не было.

## Что это

`tmux`, умерший между `shutil.which("tmux")` и `subprocess.run`, бросает
`OSError` мимо всех guard'ов функции — `_ring` падал, а через него падал
`pair_send`. То есть звонок (оптимизация) ломал доставку письма (гарантия),
которую он только сопровождал. Критик opencode подтвердил на HEAD `3607268`,
что guard всё ещё отсутствует.

## Что сделано (3607268+, раунд 48)

probe обёрнут в `try/except OSError`; отказ возвращается честным результатом
`{"rung": False, "why": "панель соседа не проверяется: …"}`, а не исключением.
Тест `test_ring_probe_oserror_is_reported_not_raised`
(`test_protocol.py`, класс `TestRingAndUnblock`, строки 3567-3575) подменяет
`subprocess.run` на бросающий OSError.
