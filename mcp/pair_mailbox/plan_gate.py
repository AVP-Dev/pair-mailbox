"""Machine gate plan → implement: no plan artefact means no move.

Why: "plan agreed" (§9a) used to be a prompt convention — the 2026-10-11
measure showed 2 rounds in 8 where the writer sent the neighbour not a single
message before the phase moved. The gate turns convention into a check: `advance`
refuses while `.pair/plan.md` holds no plan of THIS round, and the critic holds
no ack. In `audit` only the artefact is required: there the work is findings,
and no code plan is due.

One bypass closes explicitly: a `plan-ack` from the writer itself never counts.

The module moved out of server.py by the size rule: the monolith is frozen
(scripts/file-size-baseline.tsv), and growth is banned without extraction.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PLAN_FILE = "plan.md"
GATE_FILE = "plan-gate.json"
DEFAULT_ACK_TIMEOUT_HOURS = 24

# The plan first line is the round marker. Strict match on the current round
# guards against the previous round plan: the stale-verdict principle
# (review.is_stale): silently applying another revision beats missing one.
ROUND_MARKER = re.compile(r"^\s*round\s*[:=]\s*(\d+)\s*$", re.IGNORECASE)

SECTION_PATTERNS = (
    ("goal", re.compile(r"^#{1,6}\s*(goal|цель)\b", re.IGNORECASE)),
    ("invariants", re.compile(r"^#{1,6}\s*(invariants|инварианты)\b", re.IGNORECASE)),
    ("acceptance", re.compile(r"^#{1,6}\s*(acceptance|приёмка|acceptance criteria)\b",
                              re.IGNORECASE)),
)

# A verdict note: the header carries time and role, the body the plan-ack line.
NOTE_ENTRY = re.compile(r"^##\s*(\S+)\s+—\s+(\S+)\s*$")
ACK_LINE = re.compile(r"\bplan-ack\s+round\s+(\d+)\b", re.IGNORECASE)


def _plan_path(state_dir: Path) -> Path:
    return Path(state_dir) / PLAN_FILE


def parse_plan(path: Path) -> dict[str, Any]:
    """Parse the plan: the round from the marker and the sections found.

    An unreadable/missing file is not an exception but a refusal with a cause:
    the gate tells the human in words why the move is banned.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "why": f".pair/{PLAN_FILE} unreadable: {exc}",
                "round": None, "missing": list(name for name, _ in SECTION_PATTERNS)}
    lines = text.splitlines()
    round_number: int | None = None
    for line in lines[:5]:
        match = ROUND_MARKER.match(line)
        if match:
            round_number = int(match.group(1))
            break
    found = {name for name, pattern in SECTION_PATTERNS
             if any(pattern.match(line) for line in lines)}
    missing = [name for name, _ in SECTION_PATTERNS if name not in found]
    why = ""
    if round_number is None:
        why = (f"in .pair/{PLAN_FILE} has no round marker: the first line must be "
               f"`round: N` (N — the current round number)")
    elif missing:
        why = (f"in .pair/{PLAN_FILE} has no sections: {', '.join(missing)} "
               f"(need Goal/Инварианты/Acceptance headers)")
    return {"ok": round_number is not None and not missing,
            "why": why, "round": round_number, "missing": missing}


def _parse_entry_ts(stamp: str) -> datetime | None:
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None


def critic_ack(state_dir: Path, round_number: int, writer: str,
               not_before: datetime | None) -> dict[str, Any]:
    """Look for the critic note with `plan-ack round N` no earlier than `not_before`.

    Self-ack is banned: the same-writer note never counts — otherwise the gate
    would lift on one line from whoever it constrains.
    """
    notes_dir = Path(state_dir) / "notes"
    if not notes_dir.is_dir():
        return {"ack": False, "why": "note directory unreadable"}
    for path in sorted(notes_dir.glob("verdict*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        blocks = re.split(r"^(?=##\s)", text, flags=re.MULTILINE)
        for block in blocks:
            header = block.splitlines()[0] if block.strip() else ""
            match = NOTE_ENTRY.match(header.strip())
            if not match:
                continue
            stamp, role = match.group(1), match.group(2)
            if writer and role == writer:
                continue
            ack = ACK_LINE.search(block)
            if not ack or int(ack.group(1)) != round_number:
                continue
            entry_ts = _parse_entry_ts(stamp)
            if entry_ts is not None and not_before is not None and entry_ts < not_before:
                continue
            return {"ack": True, "role": role, "at": stamp, "path": str(path)}
    return {"ack": False, "why": "no critic note with this round plan-ack"}


def _ack_timeout_hours(state_dir: Path) -> float:
    target = Path(state_dir) / GATE_FILE
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
        hours = float(payload.get("ack_timeout_hours", DEFAULT_ACK_TIMEOUT_HOURS))
    except (OSError, ValueError, TypeError):
        return DEFAULT_ACK_TIMEOUT_HOURS
    return hours if hours > 0 else DEFAULT_ACK_TIMEOUT_HOURS


def timeout_ack(state_dir: Path, obligations_root: Path, writer: str,
                plan_mtime: datetime) -> dict[str, Any]:
    """The wait window lapsed and the writer awaits nothing from the neighbour.

    The "no open writer→critic obligations" condition is mandatory: otherwise a
    sent request the critic never answered would "lapse" on its own and the
    remark would drown in silence.
    """
    try:
        from review import open_count
    except ImportError:  # pragma: no cover - review sits next in the package
        open_count = None  # type: ignore[assignment]
    waiting = 0
    if open_count is not None:
        waiting = open_count(obligations_root, writer)
    hours = _ack_timeout_hours(state_dir)
    age = (datetime.now(timezone.utc) - plan_mtime).total_seconds() / 3600.0
    if waiting:
        return {"ack": False, "waiting": waiting,
                "why": (f"the writer awaits an answer on {waiting} request(s) — "
                        f"the timeout never ticks while the remark stands unanswered")}
    if age < hours:
        return {"ack": False,
                "why": (f"the plan is written {age:.1f} h ago, the visa window "
                        f"{hours:g} h still open (override: "
                        f".pair/{GATE_FILE})")}
    return {"ack": True, "why": f"window {hours:g} h lapsed with no open requests"}


def gate(state_dir: Path, state: dict[str, Any]) -> dict[str, Any]:
    """The full gate verdict for the plan → implement move.

    Returns {ok, why, ack_via}. Exempt cases (step back, reopen, audit) are the
    caller code’s call: here only "may we move forward".
    """
    path = _plan_path(state_dir)
    plan = parse_plan(path)
    if not plan["ok"]:
        return {"ok": False, "why": plan["why"], "ack_via": None}
    round_number = int(state.get("round", 0))
    if plan["round"] != round_number:
        return {"ok": False,
                "why": (f"the plan marks round: {plan['round']}, but the round is now "
                        f"{round_number}: silently applying another plan "
                        f"beats missing one"),
                "ack_via": None}
    if state.get("mode", "fix") == "audit":
        return {"ok": True, "why": "audit: the plan is an artefact, no visa needed",
                "ack_via": "audit"}
    writer = str(state.get("writer") or "")
    try:
        plan_mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        plan_mtime = datetime.now(timezone.utc)
    ack = critic_ack(state_dir, round_number, writer, plan_mtime)
    if ack.get("ack"):
        return {"ok": True, "why": f"the critic visa {ack.get('role')} ({ack.get('at')})",
                "ack_via": "ack"}
    late = timeout_ack(state_dir, Path(state_dir) / "obligations", writer, plan_mtime)
    if late.get("ack"):
        return {"ok": True, "why": late["why"], "ack_via": "timeout"}
    return {"ok": False,
            "why": f"{late['why']}; {ack['why']}. Write the plan into .pair/{PLAN_FILE} "
                   f"and send the neighbour for visa (pair_send expects_reply).",
            "ack_via": None}
