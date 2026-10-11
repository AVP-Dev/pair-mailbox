"""Машинный гейт план → implement: без артефакта плана перехода нет.

Зачем: раньше «план согласован» (§9a) было соглашением промпта — замер
2026-10-11 показал 2 раунда из 8, где писатель не отправил соседу ни одного
сообщения до смены фазы. Гейт переводит соглашение в проверку: `advance`
отказывает, пока в `.pair/plan.md` нет плана ЭТОГО раунда, а критика —
подтверждения. В `audit` требуется только артефакт: там работа — находки,
и код-план не обязателен.

Один путь обхода закрыт явно: `plan-ack` от самого писателя не считается.

Модуль вынесен из server.py по правилу размера: монолит заморожен
(scripts/file-size-baseline.tsv), и рост запрещён без вынесения.
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

# Первая строка плана — маркер раунда. Строгое совпадение с текущим раундом
# защищает от плана прошлого раунда: тот же принцип, что у stale-вердикта
# (review.is_stale): молчаливое применение чужой ревизии хуже отсутствия.
ROUND_MARKER = re.compile(r"^\s*round\s*[:=]\s*(\d+)\s*$", re.IGNORECASE)

SECTION_PATTERNS = (
    ("goal", re.compile(r"^#{1,6}\s*(goal|цель)\b", re.IGNORECASE)),
    ("invariants", re.compile(r"^#{1,6}\s*(invariants|инварианты)\b", re.IGNORECASE)),
    ("acceptance", re.compile(r"^#{1,6}\s*(acceptance|приёмка|acceptance criteria)\b",
                              re.IGNORECASE)),
)

# Заметка-вердикт: заголовок несёт время и роль, тело — строку plan-ack.
NOTE_ENTRY = re.compile(r"^##\s*(\S+)\s+—\s+(\S+)\s*$")
ACK_LINE = re.compile(r"\bplan-ack\s+round\s+(\d+)\b", re.IGNORECASE)


def _plan_path(state_dir: Path) -> Path:
    return Path(state_dir) / PLAN_FILE


def parse_plan(path: Path) -> dict[str, Any]:
    """Разобрать план: раунд из маркера и найденные секции.

    Нечитаемый/отсутствующий файл — не исключение, а отказ с причиной:
    гейт отвечает человеку словами, почему переход запрещён.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "why": f".pair/{PLAN_FILE} не читается: {exc}",
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
        why = (f"в .pair/{PLAN_FILE} нет маркера раунда: первая строка должна быть "
               f"`round: N` (N — номер текущего раунда)")
    elif missing:
        why = (f"в .pair/{PLAN_FILE} нет секций: {', '.join(missing)} "
               f"(нужны заголовки Goal/Инварианты/Acceptance)")
    return {"ok": round_number is not None and not missing,
            "why": why, "round": round_number, "missing": missing}


def _parse_entry_ts(stamp: str) -> datetime | None:
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None


def critic_ack(state_dir: Path, round_number: int, writer: str,
               not_before: datetime | None) -> dict[str, Any]:
    """Искать заметку критика с `plan-ack round N` не раньше `not_before`.

    Self-ack запрещён: заметка того же писателя не считается — иначе гейт
    снимался бы одной строкой от того, кого он ограничивает.
    """
    notes_dir = Path(state_dir) / "notes"
    if not notes_dir.is_dir():
        return {"ack": False, "why": "каталог заметок не читается"}
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
    return {"ack": False, "why": "заметки критика с plan-ack этого раунда нет"}


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
    """Окно ожидания истекло и писатель ничего не ждёт от соседа.

    Условие «нет открытых обязательств writer→критик» обязательно: иначе
    отправленный запрос, на который крик не ответил, «истекал» бы сам и
    замечание тонуло бы в тишине.
    """
    try:
        from review import open_count
    except ImportError:  # pragma: no cover - review рядом в пакете
        open_count = None  # type: ignore[assignment]
    waiting = 0
    if open_count is not None:
        waiting = open_count(obligations_root, writer)
    hours = _ack_timeout_hours(state_dir)
    age = (datetime.now(timezone.utc) - plan_mtime).total_seconds() / 3600.0
    if waiting:
        return {"ack": False, "waiting": waiting,
                "why": (f"писатель ждёт ответа на {waiting} запрос(ов) — "
                        f"таймаут не тикает, пока замечание без ответа")}
    if age < hours:
        return {"ack": False,
                "why": (f"план написан {age:.1f} ч назад, окно ожидания визы "
                        f"{hours:g} ч ещё открыто (переопределить: "
                        f".pair/{GATE_FILE})")}
    return {"ack": True, "why": f"окно {hours:g} ч истекло без открытых запросов"}


def gate(state_dir: Path, state: dict[str, Any]) -> dict[str, Any]:
    """Полный вердикт гейта для перехода plan → implement.

    Возвращает {ok, why, ack_via}. Exempt-случаи (шаг назад, реопен, audit)
    решает вызывающий код: здесь только «можно ли переходить вперёд».
    """
    path = _plan_path(state_dir)
    plan = parse_plan(path)
    if not plan["ok"]:
        return {"ok": False, "why": plan["why"], "ack_via": None}
    round_number = int(state.get("round", 0))
    if plan["round"] != round_number:
        return {"ok": False,
                "why": (f"план помечен round: {plan['round']}, а раунд сейчас "
                        f"{round_number}: молчаливое применение чужого плана "
                        f"хуже его отсутствия"),
                "ack_via": None}
    if state.get("mode", "fix") == "audit":
        return {"ok": True, "why": "audit: план — артефакт, виза не нужна",
                "ack_via": "audit"}
    writer = str(state.get("writer") or "")
    try:
        plan_mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        plan_mtime = datetime.now(timezone.utc)
    ack = critic_ack(state_dir, round_number, writer, plan_mtime)
    if ack.get("ack"):
        return {"ok": True, "why": f"виза критика {ack.get('role')} ({ack.get('at')})",
                "ack_via": "ack"}
    late = timeout_ack(state_dir, Path(state_dir) / "obligations", writer, plan_mtime)
    if late.get("ack"):
        return {"ok": True, "why": late["why"], "ack_via": "timeout"}
    return {"ok": False,
            "why": f"{late['why']}; {ack['why']}. Напиши план в .pair/{PLAN_FILE} "
                   f"и отправь соседу на визу (pair_send expects_reply).",
            "ack_via": None}
