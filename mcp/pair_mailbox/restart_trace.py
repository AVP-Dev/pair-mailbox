"""The durable restart trace: what pairrestart did, readable on reconnect.

bin/pairrestart appends one JSON line per run to .pair/restarts.jsonl BEFORE
it kills (the writer must survive its own record). A killed role's server
comes back with this module's answer in pair_state (`restarts_last`) — the
reconnecting agent learns who killed it, when, and which pids, through the
tool it already calls, with no new read path (round 102, the round-101
channel-dark lesson).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def restarts_last(state_dir: Path) -> dict[str, Any] | None:
    """The newest restart line, or None when the file is absent or unreadable.

    Deliberately total: a broken or half-written line must never crash
    pair_state — the trace is advisory, its absence reads as "no restart
    happened", never as a failure.
    """
    path = state_dir / "restarts.jsonl"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    last: dict[str, Any] | None = None
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            last = parsed
    return last
