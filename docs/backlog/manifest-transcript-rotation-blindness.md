---
title: Манифест слеп к ротированному транскрипту (архив не читается)
found: 2026-10-14
severity: high
round: 94
status: open
---

## Где

`mcp/pair_mailbox/manifest.py` (`_transcript_rows` — только активный
`transcript.jsonl`), `bin/pairreport` (тот же источник через manifest).

## Что это

RECEIPT-94 и `.pair/rounds/94.json` заявляют `(no tasks bound)` /
`tasks_closed:[]`, хотя задачи 107–110 закрыты в раунде 94. Причина:
ротация обрезала активный файл до 38 строк; `task_closed` 107–110 и
`done` 93 лежат в `.pair/transcript-archive/1.jsonl`. Cross-check слеп
той же слепотой: пустой журнал + пустой манифест = gaps пуст = отказ
не сработал. Eye (`scripts/eye-of-sauron.sh`) архив читает, манифест —
нет. Подтверждено писателем 2026-10-14 живьём на закрытии 94.

## Что сделать

`_transcript_rows` + `transcript_closed_ids` читают активный файл ПЛЮС
`transcript-archive/*.jsonl` (прецедент eye-of-sauron.sh). Окно раунда
считать по объединённому потоку. Тест: task_closed в архивном файле →
в tasks_closed манифеста. Честная оговорка: архив может ротироваться
снова — окно от last done по объединённому потоку, не по файлу.
