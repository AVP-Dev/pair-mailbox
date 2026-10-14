"""The round task banner for the pane the agent starts in.

Why a separate module: server.py is frozen (scripts/file-size-baseline.tsv),
and monolith growth is banned without extraction. The single responsibility here
is turning state into the text the human and the agents see in the pane before
the first move. Exactly as obligations.py once moved out of server.py.

History: `pairup --task` writes the task only into state.json (bin/pairup:238-241),
and the critic in a new session stayed with no incoming event — measured 2026-10-11
(2 rounds in 8 with zero sends before the phase moved, .pair/notes/finding.md).
The banner prints into both panes at start (`writer_init`/`critic_init`).

The banner is a projection from state, not handwriting: it cannot go stale.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def render_task_banner(state: dict[str, Any]) -> str:
    """The round open tasks, one line each. None open — an empty line."""
    open_items = [item for item in (state.get("tasks") or [])
                  if isinstance(item, dict) and item.get("status") == "open"]
    if not open_items:
        return ""
    lines = ["", "=== ROUND TASK (pair) ==="]
    for item in open_items:
        lines.append(f"#{item.get('id')} [{item.get('text', '')}]")
    lines.append("Read the details: pair_digest / pair_state. The writer closes tasks.")
    return "\n".join(lines) + "\n"


def write_task_banner(state_dir: Path, state: dict[str, Any]) -> Path:
    """The banner file next to state; an empty task means an empty file, not silence."""
    target = Path(state_dir) / "task-banner.txt"
    try:
        target.write_text(render_task_banner(state), encoding="utf-8")
    except OSError:
        # A write failure must not drop the task itself: the banner is pane comfort,
        # not protocol, and its absence returns the old behaviour.
        pass
    return target
