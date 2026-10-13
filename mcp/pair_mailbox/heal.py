"""pair-heal (S4): auto-reopen done -> implement on evidence ok=false.

Why: when a check, cron or agent records evidence(ok=false) in a closed round
(phase == "done"), the system must not leave the failure unnoticed. `pair-heal`
automatically:
1. Moves the phase `done → implement` and bumps the round number (`round += 1`).
2. Adds a new task `auto-heal: <kind> (<value>)`.
3. Records a `heal_reopen` event in `transcript.jsonl`.
4. Guards against an endless self-heal loop via the attempt cap `MAX_HEAL_ATTEMPTS = 3`.
   Past the cap the auto-reopen blocks and demands a human.
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
    """Perform the auto-reopen on a failure in done."""
    state = store.read()
    current_phase = state.get("phase", "")
    if current_phase != "done":
        return {"healed": False, "why": "not in done phase"}

    heal_info = load_heal_state(store.root)
    attempts = int(heal_info.get("attempts", 0))

    if attempts >= MAX_HEAL_ATTEMPTS:
        msg = f"self-heal cap ({MAX_HEAL_ATTEMPTS}) spent; a human must step in"
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

        # The auto-heal task pins the reopen cause in the round task registry
        tasks = st.setdefault("tasks", [])
        next_id = max([int(t.get("id", 0)) for t in tasks] or [0]) + 1
        new_task = {
            "id": next_id,
            "text": heal_task_text,
            "status": "open",
            "added_at": now_iso,
            "added_by": f"heal:{role}",
            "round": new_round,
            "reason": "auto-created by pair-heal on negative evidence",
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
