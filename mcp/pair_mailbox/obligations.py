"""Reply obligations: who awaits whom, who already answered whom.

Moved out of `server.py` (the server-py-still-a-monolith card): a request with
`expects_reply` plus its answer is one responsibility, revision and transcript with
it. One dependency — the store (roots, project_root, transcript), no back references
to PairSession, so server imports from here, never the reverse.
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
    # Binding the request to the revision (review-queue): HEAD + dirty-hash + files.
    # With no it a verdict arriving past new commits would silently land on
    # unchecked code. Fail-open: with no git an empty snapshot writes.
    try:
        from review import snapshot
        record["rev"] = snapshot(store.project_root)
    except Exception:  # noqa: BLE001 - the snapshot must never drop the request
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
    # One implementation per project — in server.py. A lazy import: obligations
    # imports from server at top level, a reverse top-level import would loop;
    # by call time server already loads.
    from server import atomic_write_json
    atomic_write_json(path, payload)
