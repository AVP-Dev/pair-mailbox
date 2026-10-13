"""Task Receipt: машинный чек-отчет раунда (.pair/RECEIPT-<round>.md).

Зачем: по завершении раунда (§10, done) должен оставаться неизменяемый артефакт,
фиксирующий:
- Номер раунда, задачу раунда;
- Кто был писателем и критиком;
- Закрытые задачи раунда (tasks);
- Собранные доказательства (evidences);
- Фактические метрики бюджета (сообщения, переходы, часы, рестарты);
- Хэш HEAD и дату закрытия.
- [R82] Секцию Ground Truth: dirty_hash фактов, вердикт anti-mock
  (PASS/BLOCKED + число), ссылку на запись ledger. Квитанция fail-closed:
  при боевых моках квитанция не пишется, а бросается GroundTruthBlocked.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class GroundTruthBlocked(Exception):
    """Факты запрещают квитанцию: боевые моки или расхождение с diff."""




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

    # [R82] Ground Truth: факты живьём из корня репозитория, вердикт гейта,
    # ссылка на ledger. Fail-closed: боевые моки — исключение до записи.
    ground_lines = _ground_truth_lines(state_dir, round_number)
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
        "## Ground Truth",
        *ground_lines,
        "",
    ])

    content = "\n".join(lines)
    target.write_text(content, encoding="utf-8")
    return target


def _ground_truth_lines(state_dir: Path, round_number: int) -> list[str]:
    """Секция Ground Truth: факты, вердикт, ledger. Блок — исключением."""
    import anti_mock_gate
    import session_facts
    from ledger import read_round
    repo = state_dir.resolve()
    while not (repo / ".git").is_dir() and repo.parent != repo:
        repo = repo.parent
    if not (repo / ".git").is_dir():
        # Вне репозитория фактов нет — UNKNOWN, не блок (договор: PRESERVE).
        return ["- facts: UNKNOWN (state вне git-дерева)"]
    facts = session_facts.collect_facts(repo)
    verdict = anti_mock_gate.judge(facts)
    if not verdict["ok"]:
        first = verdict["blockers"][0]
        raise GroundTruthBlocked(
            f"anti-mock BLOCKED ({len(verdict['blockers'])}): "
            f"{first['file']}:{first['line']} [{first['pattern']}]")
    ledger_ref = "—"
    try:
        records = read_round(state_dir, round_number)
        if records:
            ledger_ref = f"ledger.jsonl: {len(records)} запис(ь/ей) раунда {round_number}"
    except Exception:
        pass
    return [
        f"- dirty_hash: {facts.get('dirty_hash', '—')}",
        f"- anti-mock: PASS ({len(verdict['blockers'])} blockers, "
        f"{len(verdict['warnings'])} warnings)",
        f"- ledger: {ledger_ref}",
    ]

