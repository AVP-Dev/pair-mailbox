---
title: declare -A в file-size-gate.sh против штатного bash 3.2 на macOS
found: 2026-10-14
severity: low
round: 92
status: open
---

## Где

`scripts/file-size-gate.sh:128-129` (`declare -A allowed_max/reason`),
shebang `#!/usr/bin/env bash`.

## Что это

Ассоциативные массивы — bash 4+; в macOS штатный /bin/bash — 3.2, гейт там
упадёт с syntax error. GNU-флагов (stat -c/date -d/readlink -f) в боевом коде
при этом не найдено; python-floor честный (3.8+). Подтверждено писателем
2026-10-14.

## Что сделать

Либо переписать без ассоциативных массивов (POSIX-совместимо), либо
shebang/guard на `bash >= 4` с понятной ошибкой + строка в README про
`brew install bash` для Mac.
