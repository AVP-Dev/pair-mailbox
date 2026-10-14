"""Machine gate scoping -> plan: intent pinned in .pair/intent.md.

Why: moving to plan with no stated problem and no task bounds drifts focus
and commits early. The gate demands a `.pair/intent.md` artefact with the
current-round marker `round: N` and mandatory sections:
- problem (problem)
- scope (scope)
- outcome (outcome/acceptance)

In `audit` mode the gate also demands the artefact (problem research).
Steps back and repeated moves (reopen) stay out of the block.
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
    """Parse intent.md: check the round marker and the key sections."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return {
            "ok": False,
            "why": f".pair/{INTENT_FILE} unreadable: {exc}",
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
            f"in .pair/{INTENT_FILE} has no round marker: the first line must be "
            f"`round: N` (N — the current round number)"
        )
    elif missing:
        why = (
            f"in .pair/{INTENT_FILE} misses sections: {', '.join(missing)} "
            f"(need Problem, Scope, Outcome headers)"
        )

    return {
        "ok": round_number is not None and not missing,
        "why": why,
        "round": round_number,
        "missing": missing,
    }


def gate(state_dir: Path, state: dict[str, Any]) -> dict[str, Any]:
    """Gate check on the scoping → plan move.

    Returns {"ok": bool, "why": str}.
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
                f"intent.md marks round: {res['round']}, but the current round is {round_number}. "
                f"The intent artefact must match the current round."
            ),
        }

    return {"ok": True, "why": "intent.md checked: round marker and sections in order"}
