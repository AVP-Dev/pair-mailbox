---
title: /proc-зависимости молча деградируют на macOS
found: 2026-10-14
severity: medium
round: 92
status: open
---

## Где

`mcp/pair_mailbox/composer.py:324,416,453`, `board_hosts.py:115-127`,
`server_freshness.py:24`, `bin/pairdoctor:96-99`.

## Что это

Каждый /proc-читатель глотает OSError: pane discovery, descendants и
freshness на macOS (где /proc нет) деградируют silently — пустые списки,
fallback на time.time, пропуск pid. Код честно fail-closed построчно, но ни
один путь не говорит «платформа без /proc, данные недоступны».
Подтверждено писателем 2026-10-14 чтением всех четырёх точек.

## Что сделать

Единый helper (напр. `proc_available()`), громкий warn в pairdoctor и
dashboard при отсутствии /proc; платформенный fallback (ps/lsof) — отдельной
картой, если понадобится настоящий Mac-порт.
