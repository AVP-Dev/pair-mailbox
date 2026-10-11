"""Машинный гейт scoping → plan: фиксация намерений в .pair/intent.md.

Зачем: переход в plan без сформулированной проблемы и границ задачи приводит
к расфокусировке и преждевременным коммитам. Гейт требует наличия артефакта
`.pair/intent.md` с маркером текущего раунда `round: N` и обязательными секциями:
- problem (проблема/problem)
- scope (границы/scope)
- outcome (ожидаемый результат/outcome/acceptance)

В режиме `audit` гейт также требует артефакт (исследование проблемы).
Шаги назад и повторные переходы (reopen) исключены из блокировки.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

INTENT_FILE = "intent.md"

ROUND_MARKER = re.compile(r"^\s*round\s*[:=]\s*(\d+)\s*$", re.IGNORECASE)

SECTION_PATTERNS = (
    ("problem", re.compile(r"^#{1,6}\s*(problem|проблема)\b", re.IGNORECASE)),
    ("scope", re.compile(r"^#{1,6}\s*(scope|границы|границы задачи)\b", re.IGNORECASE)),
    ("outcome", re.compile(r"^#{1,6}\s*(outcome|ожидаемый результат|результат)\b", re.IGNORECASE)),
)


def _intent_path(state_dir: Path) -> Path:
    return Path(state_dir) / INTENT_FILE


def parse_intent(path: Path) -> dict[str, Any]:
    """Разобрать intent.md: проверить маркер раунда и ключевые секции."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return {
            "ok": False,
            "why": f".pair/{INTENT_FILE} не читается: {exc}",
            "round": None,
            "missing": [name for name, _ in SECTION_PATTERNS],
        }
    lines = text.splitlines()
    round_number: int | None = None
    for line in lines[:5]:
        match = ROUND_MARKER.match(line)
        if match:
            round_number = int(match.group(1))
            break

    found = {
        name
        for name, pattern in SECTION_PATTERNS
        if any(pattern.match(line) for line in lines)
    }
    missing = [name for name, _ in SECTION_PATTERNS if name not in found]

    why = ""
    if round_number is None:
        why = (
            f"в .pair/{INTENT_FILE} нет маркера раунда: первая строка должна быть "
            f"`round: N` (N — номер текущего раунда)"
        )
    elif missing:
        why = (
            f"в .pair/{INTENT_FILE} отсутствуют секции: {', '.join(missing)} "
            f"(нужны заголовки Проблема/Problem, Границы/Scope, Ожидаемый результат/Outcome)"
        )

    return {
        "ok": round_number is not None and not missing,
        "why": why,
        "round": round_number,
        "missing": missing,
    }


def gate(state_dir: Path, state: dict[str, Any]) -> dict[str, Any]:
    """Проверка гейта при переходе scoping → plan.

    Возвращает {"ok": bool, "why": str}.
    """
    path = _intent_path(state_dir)
    res = parse_intent(path)
    if not res["ok"]:
        return {"ok": False, "why": res["why"]}

    round_number = int(state.get("round", 0))
    if res["round"] != round_number:
        return {
            "ok": False,
            "why": (
                f"intent.md помечен round: {res['round']}, а текущий раунд {round_number}. "
                f"Артефакт намерения должен соответствовать текущему раунду."
            ),
        }

    return {"ok": True, "why": "intent.md проверен: маркер раунда и секции в порядке"}
