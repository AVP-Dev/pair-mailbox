"""Task Receipt: машинный чек-отчет раунда (.pair/RECEIPT-<round>.md).

Зачем: по завершении раунда (§10, done) должен оставаться неизменяемый артефакт,
фиксирующий:
- Номер раунда, задачу раунда;
- Кто был писателем и критиком;
- Закрытые задачи раунда (tasks);
- Собранные доказательства (evidences);
- Фактические метрики бюджета (сообщения, переходы, часы, рестарты);
- Хэш HEAD и дату закрытия.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def generate_receipt(state_dir: Path, state: dict[str, Any], head_commit: str = "") -> Path:
    """Сгенерировать и записать .pair/RECEIPT-<round>.md."""
    state_dir = Path(state_dir)
    round_number = int(state.get("round", 0))
    target = state_dir / f"RECEIPT-{round_number}.md"

    writer = state.get("writer", "—")
    roles = state.get("roles", {})
    peers = [r for r in roles if r != writer]

    # Сбор задач раунда
    tasks = state.get("tasks", [])
    round_tasks = [t for t in tasks if t.get("round") == round_number]

    # Сбор доказательств раунда
    evidences = state.get("evidence", [])
    round_evidences = [e for e in evidences if e.get("round") == round_number]

    # Метрики бюджета
    metrics_str = "—"
    try:
        from budget import collect_metrics
        m = collect_metrics(state_dir, state)
        metrics_str = (
            f"- Сообщения: {m['messages']}\n"
            f"- Переходы (advances): {m['advances']}\n"
            f"- Время раунда: {m['hours']} ч.\n"
            f"- Рестарты пилота: {m['restarts']}"
        )
    except Exception:
        pass

    now_iso = datetime.now(timezone.utc).isoformat()

    lines = [
        f"# Task Receipt — Round {round_number}",
        "",
        f"- **Closed at**: {now_iso}",
        f"- **Task**: {state.get('task', '—')}",
        f"- **Writer**: {writer}",
        f"- **Peers**: {', '.join(peers) or '—'}",
        f"- **Mode**: {state.get('mode', 'fix')}",
        f"- **Autonomy**: {state.get('autonomy', 'auto')}",
        f"- **Head Commit**: {head_commit or '—'}",
        "",
        "## Tasks Closed in Round",
    ]

    if round_tasks:
        for t in round_tasks:
            status_mark = "x" if t.get("status") == "done" else " "
            lines.append(f"- [{status_mark}] #{t.get('id')}: {t.get('text')} ({t.get('status')})")
    else:
        lines.append("*(нет задач, привязанных к раунду)*")

    lines.extend([
        "",
        "## Evidences",
    ])

    if round_evidences:
        for e in round_evidences:
            ok_mark = "OK" if e.get("ok") else "FAIL"
            lines.append(f"- [{ok_mark}] {e.get('kind')}: {e.get('value')} ({e.get('role')})")
    else:
        lines.append("*(доказательства отсутствуют)*")

    lines.extend([
        "",
        "## Budget Metrics",
        metrics_str,
        "",
    ])

    content = "\n".join(lines)
    target.write_text(content, encoding="utf-8")
    return target
