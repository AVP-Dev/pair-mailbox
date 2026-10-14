---
title: Eye alarm маскируется обязательствами; deadlock не даёт ALARM
found: 2026-10-14
severity: high
round: 92
status: open
---

## Где

`scripts/eye-of-sauron.sh:75` (`alarm = (phase != "done" and ws > 1200 and obl == 0)`),
ветка deadlock ниже (текст без is_alarm), `.pair/EYE.log`.

## Что это

Сторож, отвечающий «писатель жив?», молчит ровно когда вопрос важен: фазу
done гасила до 19:43Z (лог 22:40 ph=done), теперь гасит obl=2 (лог 22:50:
w_sil=28531s ≈ 7.9 ч, всё равно ok). Ветка deadlock дописывает слово в
строку, но префикс ALARM не ставит — висяк с обязательствами никогда не
алармит. Подтверждено писателем 2026-10-14 чтением кода и лога.

## Что сделать

`is_alarm = alarm or budget_alarm or (deadlock != "")`: deadlock-слово
обязано давать префикс ALARM. Тест: obl>0 + оба молчат >1200с + хэши панели
стоят → строка с ALARM DEADLOCK.
