"""The round's subject, in one place, for every surface that shows it.

`state["task"]` is a legacy scalar from round 98, written only by the human's
`pairctl task` (`pairctl_authority.cmd_task`), which also appends to the `tasks`
list it now duplicates. Nothing rewrites the scalar when a round advances, so it
can only ever go stale — and a stale subject is worse than no subject, because
every orientation surface reads it: `pair_state`, `pair_digest`, the tmux board
snapshot, `pairctl status`. Round 103 worked round 103 while all four printed
"Round 98: …" as the subject without a word.

This module holds the single rule, and the frozen files (server.py,
pairctl_authority.py, board_snapshot.py, digest.py each carry a hard line cap)
import from here instead of growing a private copy. The copies are exactly how
the defect shipped twice: digest gained the guard in round 98, and the other
three surfaces never had it.

No imports from server: every caller is reachable from this one, so a direct
import would cycle. The state shape is the contract — `tasks`, `task`, `round`.
"""
from __future__ import annotations


def round_mark(state: dict) -> str:
    """How a task string names the round it belongs to: "round 103"."""
    return f"round {state.get('round', 0)}"


def open_subject(state: dict) -> str:
    """The first open task's text, or "" — what stands for the subject today."""
    for item in state.get("tasks") or []:
        if not isinstance(item, dict):
            continue
        if item.get("status") != "open":
            continue
        text = str(item.get("text") or "").strip()
        if text:
            return text
    return ""


def scalar_names_round(state: dict) -> bool:
    """True when state["task"] names this round: only then is it the subject."""
    raw = str(state.get("task") or "").strip()
    return bool(raw) and round_mark(state) in raw.lower()


def subject_line(state: dict) -> str:
    """The round's subject, or a prefix named stale when the scalar is not it.

    An honest answer beats a confident wrong one, which is why the scalar is
    never dropped: when it cannot pose as the subject it still travels, marked
    "stale — not round N", so the human can see the drift instead of guessing.
    """
    raw = str(state.get("task") or "").strip()
    first_open = open_subject(state)
    if scalar_names_round(state):
        return raw
    if first_open:
        return first_open
    if raw:
        return f"{raw} (stale — not round {state.get('round', 0)})"
    return ""
