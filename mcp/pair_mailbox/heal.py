"""pair-heal (S4): авто-реопен done -> implement при evidence ok=false.

Зачем: если проверка, cron или агент фиксирует evidence(ok=false) в закрытом
раунде (phase == "done"), система не должна оставлять сбой незамеченным.
`pair-heal` автоматически:
1. Переводит фазу `done → implement` и поднимает номер раунда (`round += 1`).
2. Добавляет новую задачу `auto-heal: <kind> (<value>)`.
3. Записывает событие `heal_reopen` в `transcript.jsonl`.
4. Защищает от бесконечного цикла самоисцеления через лимит попыток `MAX_HEAL_ATTEMPTS = 3`.
   При превышении попыток авто-реопен блокируется с требованием вмешательства человека.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MAX_HEAL_ATTEMPTS = 3
HEAL_STATE_FILE = "heal-state.json"


def load_heal_state(state_dir: Path) -> dict[str, Any]:
    target = Path(state_dir) / HEAL_STATE_FILE
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"attempts": 0, "last_heal": None}


def save_heal_state(state_dir: Path, data: dict[str, Any]) -> None:
    target = Path(state_dir) / HEAL_STATE_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, indent=2), encoding="utf-8")


def reset_heal_state(state_dir: Path) -> None:
    save_heal_state(state_dir, {"attempts": 0, "last_heal": None})


def trigger_heal(
    store: Any,
    role: str,
    record: dict[str, Any],
) -> dict[str, Any]:
    """Выполнить авто-реопен при сбое в done."""
    state = store.read()
    current_phase = state.get("phase", "")
    if current_phase != "done":
        return {"healed": False, "why": "не в фазе done"}

    heal_info = load_heal_state(store.root)
    attempts = int(heal_info.get("attempts", 0))

    if attempts >= MAX_HEAL_ATTEMPTS:
        msg = f"лимит авто-исцелений ({MAX_HEAL_ATTEMPTS}) исчерпан; требуется вмешательство человека"
        store.append_transcript("heal_exhausted", role, attempts=attempts, error=record.get("value"))
        return {"healed": False, "why": msg, "exhausted": True}

    new_attempts = attempts + 1
    new_round = int(state.get("round", 0)) + 1
    now_iso = datetime.now(timezone.utc).isoformat()

    heal_task_text = f"auto-heal: {record.get('kind', 'test')} failed — {record.get('value', '')[:100]}"

    def apply(st: dict[str, Any]) -> dict[str, Any]:
        st["phase"] = "implement"
        st["round"] = new_round
        st["reopened_by"] = f"heal:{role}"
        st.pop("closed_at", None)

        # Добавим задачу на исправление
        tasks = st.setdefault("tasks", [])
        next_id = max([int(t.get("id", 0)) for t in tasks] or [0]) + 1
        new_task = {
            "id": next_id,
            "text": heal_task_text,
            "status": "open",
            "added_at": now_iso,
            "added_by": f"heal:{role}",
            "round": new_round,
            "reason": "авто-создано pair-heal по отрицательному evidence",
            "closed_at": None,
            "closed_by": None,
        }
        tasks.append(new_task)
        return new_task

    created_task = store.mutate(apply)
    save_heal_state(store.root, {"attempts": new_attempts, "last_heal": now_iso})

    store.append_transcript(
        "heal_reopen",
        role,
        round=new_round,
        attempts=new_attempts,
        task_id=created_task["id"],
        evidence_value=record.get("value", "")[:200],
    )

    return {
        "healed": True,
        "round": new_round,
        "attempts": new_attempts,
        "task": created_task,
    }
