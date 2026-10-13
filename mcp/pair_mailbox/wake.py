"""Event wake-up without the composer: a file instead of printing.

The wake-without-composer card: `tmux send-keys` is an optimization, not a
working condition. The data is already solid (mailbox/notes/task/transcript),
only the trigger is fragile. This module is the trigger file: the server writes
`.pair/wake/<role>.json` on every event (new letter, note), and `bin/pairpilot`
polls it in `poll_wakes`, printing only past `wake_grace` when the role never
woke on its own. A print failure degrades into a file, not into silence.

The file format matches what `Pilot.mark_wake` writes, so the pilot poller
parses it unchanged: `role/reason/reasons/asked_at/seen_at_ask/unread_at_ask/text`
(+ the kept `escalated`). Merging with a hanging file never rewrites
`asked_at`/`seen_at_ask`, or patience would restart and one fact would escalate
twice (fixed in round 15).
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
    """Record the "wake the role" fact — into a file, not the composer.

    Never throws past: unwritten means the caller writes the cause into the
    transcript itself, while the event (letter/note) already sits in the container.
    Returns the file path for tests.
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
