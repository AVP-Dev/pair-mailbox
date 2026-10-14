---
title: pair-chat не установлен ни в один глобальный skills-дир
found: 2026-10-14
severity: medium
round: 92
status: open
---

## Где

`skills/pair-chat/SKILL.md` (источник есть), `install.sh:303-311` (write_skills).

## Что это

Шесть глобальных диров (`~/.config/opencode/skills`, `~/.omp/agent/skills`,
`~/.codex/skills`, `~/.claude/skills`, `~/.pi/agent/skills`,
`~/.gemini/config/skills`) несут pair-audit/pair-backlog/pair-work/...,
но не pair-chat. Только repo-local `.opencode/skills` и `.agents/skills`
его имеют. Глобальные диры старше pair-chat и не переустанавливались.
`bin/pairdoctor:186-193` предупреждает раз на роль — сигнал есть, но лечится
только переустановкой. Подтверждено писателем 2026-10-14 прямым `ls`.

## Что сделать

Перезапустить `install.sh` (write_skills копирует всё из `skills/`) на всех
шести CLI; либо задокументировать pair-chat как repo-local по дизайну.
