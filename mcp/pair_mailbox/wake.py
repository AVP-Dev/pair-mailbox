"""Событийное пробуждение без композера: файл вместо печати.

Карточка wake-without-composer: `tmux send-keys` — оптимизация, а не условие
работы. Данные уже надёжны (mailbox/notes/task/transcript), хрупок только
триггер. Этот модуль — триггер-файл: сервер пишет `.pair/wake/<role>.json`
на каждое событие (новое письмо, заметка), а `bin/pairpilot` опрашивает его
в `poll_wakes` и печатает только после `wake_grace`, если роль не проснулась
сама. Неудача печати деградирует в файл, а не в тишину.

Формат файла — тот же, что пишет `Pilot.mark_wake`, чтобы опросник пилота
понимал его без изменений: `role/reason/reasons/asked_at/seen_at_ask/
unread_at_ask/text` (+ сохранённый `escalated`). Слияние с висящим файлом
не перезаписывает `asked_at`/`seen_at_ask`, иначе терпение начиналось бы
заново и один факт эскалировался бы дважды (исправлено в раунде 15).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def wake_path(state_dir: Path, role: str) -> Path:
    return Path(state_dir) / "wake" / f"{role}.json"


def count_unread(state: dict[str, Any], state_dir: Path, role: str) -> int:
    try:
        inbox = Path(state_dir) / "mailbox" / role
        total = len(list(inbox.glob("*.json"))) if inbox.is_dir() else 0
    except OSError:
        total = 0
    try:
        seen = int((state.get("seen") or {}).get(role, 0))
    except (TypeError, ValueError):
        seen = 0
    return max(0, total - seen)


def record_wake(state_dir: Path, state: dict[str, Any], role: str,
                reason: str, text: str) -> Path:
    """Записать факт «роль надо разбудить» — в файл, а не в композер.

    Никогда не бросает мимо: не записали — вызывающий пишет причину
    в транскрипт сам, а событие (письмо/заметка) уже лежит в контейнере.
    Возвращает путь к файлу для тестов.
    """
    path = wake_path(state_dir, role)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = {}
    if not isinstance(previous, dict):
        previous = {}
    prior = previous.get("reasons")
    if not isinstance(prior, list):
        prior = [previous["reason"]] if previous.get("reason") else []
    seen_reasons = [str(r) for r in prior]
    if reason not in seen_reasons:
        seen_reasons.append(reason)
    payload = dict(previous)
    payload.update({
        "role": role,
        "reason": seen_reasons[-1],
        "reasons": seen_reasons,
        "asked_at": previous.get("asked_at") or utcnow(),
        "seen_at_ask": previous.get("seen_at_ask",
                                    (state.get("roles") or {}).get(role, {}).get("last_seen")),
        "unread_at_ask": int(previous.get("unread_at_ask",
                                          count_unread(state, state_dir, role))),
        "text": text,
    })
    payload.setdefault("asked_at", utcnow())
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass
    return path
