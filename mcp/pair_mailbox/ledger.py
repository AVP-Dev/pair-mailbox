"""Round ledger: структурированная запись решений раунда.

Зачем: итоги раунда лежат в одном машиночитаемом месте — 5 срезов
(decisions, touchpoints, obligations, evidence, debts). Пустые поля —
пустые, не выдуманные (тот же договор, что у receipt.py). Формат —
JSONL: одна запись на строку, хвост читается без разбора всего файла.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LEDGER_FILE = "ledger.jsonl"
SLICES = ("decisions", "touchpoints", "obligations", "evidence", "debts")


def _ledger_path(state_dir: Path | str) -> Path:
    return Path(state_dir) / LEDGER_FILE


def append(state_dir: Path | str, round_number: int, role: str,
           entry: dict[str, Any]) -> dict[str, Any]:
    """Добавить запись: только известные срезы, остальное отбрасывается."""
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "round": int(round_number),
        "role": role,
    }
    for name in SLICES:
        record[name] = entry.get(name, [])
    target = _ledger_path(state_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def read_round(state_dir: Path | str, round_number: int,
               tail: int = 50) -> list[dict[str, Any]]:
    """Прочитать хвост леджера, отфильтровать по раунду."""
    target = _ledger_path(state_dir)
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in lines[-tail:]:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("round") == int(round_number):
            out.append(record)
    return out
