"""Баннер задачи раунда для панели, в которой агент начинается.

Зачем отдельный модуль: server.py заморожен (scripts/file-size-baseline.tsv),
и рост монолита запрещён без вынесения. Здесь живёт единственная ответственность —
превращать состояние в текст, который человек и агенты видят в панели до первого
хода. Ровно как obligations.py вынесены из server.py в своё время.

История: `pairup --task` пишет задачу только в state.json (bin/pairup:238-241), и
критик в новой сессии оставался без входного события — замерено 2026-10-11
(2 раунда из 8 без единого send'а до смены фазы, .pair/notes/finding.md).
Баннер печатается в обе панели при старте (`writer_init`/`critic_init`).

Баннер — проекция из состояния, а не рукописный текст: устареть не может.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def render_task_banner(state: dict[str, Any]) -> str:
    """Открытые задачи раунда одной строкой каждая. Нет открытых — пустая строка."""
    open_items = [item for item in (state.get("tasks") or [])
                  if isinstance(item, dict) and item.get("status") == "open"]
    if not open_items:
        return ""
    lines = ["", "=== ЗАДАЧА РАУНДА (pair) ==="]
    for item in open_items:
        lines.append(f"#{item.get('id')} [{item.get('text', '')}]")
    lines.append("Прочитай подробности: pair_digest / pair_state. Задачи закрывает писатель.")
    return "\n".join(lines) + "\n"


def write_task_banner(state_dir: Path, state: dict[str, Any]) -> Path:
    """Файл-баннер рядом с состоянием; пустая задача — пустой файл, не молчание."""
    target = Path(state_dir) / "task-banner.txt"
    try:
        target.write_text(render_task_banner(state), encoding="utf-8")
    except OSError:
        # Ошибка записи не должна ронять саму задачу: баннер — удобство
        # панели, а не протокол, и его отсутствие возвращает прежнее поведение.
        pass
    return target
