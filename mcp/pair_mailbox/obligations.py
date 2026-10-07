"""Обязательства ответа: кто кого ждёт и кто кому уже ответил.

Вынесено из `server.py` (карточка server-py-still-a-monolith): запрос с
`expects_reply` и ответ на него — одна ответственность, ревизия и транскрипт
при ней. Зависимость одна — store (корни, project_root, транскрипт), обратных
ссылок на PairSession нет, поэтому server импортирует отсюда, а не наоборот.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def list_obligations(store: Any, side: str, role: str | None) -> list[dict[str, Any]]:
    """Open obligations, optionally filtered to one role.

    Kept small on purpose: a long list of stale obligations is noise, and the
    supervisor is what actually chases them.
    """
    out: list[dict[str, Any]] = []
    if not store.obligations_root.is_dir():
        return out
    for path in sorted(store.obligations_root.glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if role is not None and record.get(side) != role:
            continue
        out.append({"seq": record.get("seq"), "from": record.get("from"),
                    "to": record.get("to"), "opened_at": record.get("opened_at"),
                    "subject": record.get("subject", "")})
    return out[:20]


def open_obligation(store: Any, frm: str, to: str, seq: int, subject: str = "") -> dict[str, Any]:
    """Record that `frm` is waiting for an answer from `to`.

    The protocol's blind spot was exactly this: a message asking for a decision was
    indistinguishable from small talk, so a request nobody answered looked like a quiet
    evening. An obligation is a plain file, so the supervisor can read it without
    understanding any of the conversation.
    """
    store.obligations_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = store.obligations_root / f"{seq:06d}-{stamp}-{frm}-{to}.json"
    record = {"seq": seq, "from": frm, "to": to, "opened_at": _utcnow(), "subject": subject}
    # Привязка запроса к ревизии (review-queue): HEAD + dirty-hash + файлы.
    # Без неё вердикт, пришедший после новых коммитов, молча ложился бы
    # на непроверенный код. Fail-open: без git пишется пустой снимок.
    try:
        from review import snapshot
        record["rev"] = snapshot(store.project_root)
    except Exception:  # noqa: BLE001 - снимок не должен ронять запрос
        record["rev"] = {"head": "", "dirty": "", "files": []}
    _atomic_write_json(path, record)
    store.append_transcript("obligation_opened", frm, to=to, seq=seq, subject=subject)
    return record


def answer_obligations(store: Any, answerer: str, author: str) -> list[int]:
    """Called when `answerer` writes to `author`: whatever they owed each other closes.

    Any later message counts as the answer, including a fresh request, because two agents
    talking have by definition stopped waiting on each other. Judging the content would be
    guesswork, and a wrong guess here means a real request gets nagged about at 3am.
    """
    closed: list[int] = []
    roots = sorted(store.obligations_root.glob("*.json")) if store.obligations_root.is_dir() else []
    for path in roots:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if record.get("from") == answerer and record.get("to") == author:
            path.unlink(missing_ok=True)
            closed.append(int(record.get("seq", 0)))
    if closed:
        store.append_transcript("obligation_answered", answerer, author=author, seqs=closed)
    return closed

def _atomic_write_json(path: Path, payload: Any) -> None:
    # Одна реализация на проект — в server.py. Ленивый импорт: obligations
    # импортируется из server на верхнем уровне, обратный импорт на верхнем
    # уровне был бы циклом; к моменту вызова server уже загружен.
    from server import atomic_write_json
    atomic_write_json(path, payload)
