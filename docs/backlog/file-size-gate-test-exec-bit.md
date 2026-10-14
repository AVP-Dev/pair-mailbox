---
title: Исполняемый бит scripts/file-size-gate_test.sh
found: 2026-10-14
severity: low
round: 92
status: open
---

## Где

`scripts/file-size-gate_test.sh` (mode 664; sibling-скрипты 775).

## Что это

Прямой запуск `./scripts/file-size-gate_test.sh` умирает `Permission denied`
(exit 126). CI спасают две случайности: `scripts/verification-profile.tsv:65`
зовёт через `bash`, а shell-syntax список файл опускает. Любой человек по
репозиторийному паттерну `./scripts/x_test.sh` получает 126. Подтверждено
писателем 2026-10-14: прямой запуск denied, `bash` — 30 checks passed.

## Что сделать

`chmod +x scripts/file-size-gate_test.sh` + добавить файл в shell-syntax
список verification-profile, чтобы бит проверялся, а не спасался случаем.
