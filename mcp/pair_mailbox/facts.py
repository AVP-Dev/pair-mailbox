"""The facts store: the anchor that survives a compaction.

Measured on the harness's own sessions, 2026-10-21: a compaction removes about 90 % of
the context and leaves 39 513 to 49 131 tokens of prose alive — the summary the agent
carries for the rest of the session. It cannot be queried, diffed or dropped. This is
the same anchor in a shape an agent can read for 500 tokens: key → value, bounded, and
a write that would cross the budget is REFUSED rather than quietly trimmed.

Where the facts live: `.pair/facts.json`, a file of its own. Not `state.json`: that
one rotates (round 127), and a fact that vanishes with a rotation is not an anchor.

Who writes: any agent, like a note — the critic is often the one who knows a new fact.
The writer owns the pruning: dropping and clearing are the writer's.

At round close the facts stay: they are the next round's starting point.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from token_budget import FACTS_TOKEN_BUDGET, estimate_tokens
from store_core import atomic_write_json, utcnow

FACTS_FILE = "facts.json"
# A fact is a short line, not a note. The value cap keeps one fact from eating the
# whole budget and the room from carrying a second anchor in prose.
VALUE_MAX = 400
KEY_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")


class FactsError(Exception):
    """A fact that may not be written. The server turns it into a ProtocolError."""


def facts_path(store: Any) -> Path:
    """Where the facts live: beside the state, not inside it."""
    return Path(store.root) / FACTS_FILE


def read_facts(store: Any) -> list[dict[str, Any]]:
    """The stored facts, in the order they were first written.

    A broken file reads as no facts, not as a crash: the anchor is a convenience,
    and a convenience that stops the pair from working is not one.
    """
    path = facts_path(store)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    facts = data.get("facts") if isinstance(data, dict) else None
    return [item for item in facts if isinstance(item, dict)] if isinstance(facts, list) else []


def write_facts(store: Any, facts: list[dict[str, Any]]) -> None:
    path = facts_path(store)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, {"facts": facts, "updated_at": utcnow()})


def render_facts(facts: list[dict[str, Any]]) -> str:
    """The facts as an agent reads them: one line each, `key: value`."""
    return "\n".join(f"{item.get('key')}: {item.get('value')}" for item in facts)


def facts_tokens(facts: list[dict[str, Any]]) -> int:
    """What the facts cost to read."""
    return estimate_tokens(render_facts(facts))


def set_fact(store: Any, key: str, value: str, role: str, round_number: int = 0) -> dict[str, Any]:
    """Set or replace one fact. Refuses a write that would cross the budget.

    The refusal is the point. A store that silently trims a fact is a store that
    loses the fact the writer meant to record, and the agent goes on believing the
    old one. The budget is a ceiling the writer can see and clear.
    """
    key = (key or "").strip()
    value = (value or "").strip()
    if not KEY_PATTERN.fullmatch(key):
        raise FactsError(
            "`key` must be a short slug: letters, digits, dot, dash, underscore.")
    if not value:
        raise FactsError("`value` is required: a fact without a value is a label.")
    if len(value) > VALUE_MAX:
        raise FactsError(f"A fact longer than {VALUE_MAX} characters is not a fact.")
    with store.lock():
        facts = read_facts(store)
        kept = [item for item in facts if item.get("key") != key]
        kept.append({"key": key, "value": value, "by": role, "at": utcnow(),
                     "round": int(round_number)})
        tokens = facts_tokens(kept)
        if tokens > FACTS_TOKEN_BUDGET:
            raise FactsError(
                f"the facts would cost {tokens} tokens, over the {FACTS_TOKEN_BUDGET}-token "
                f"budget. Drop a fact before adding one — the anchor is meant to be small, "
                f"or it is the prose anchor with a smaller number on it.")
        write_facts(store, kept)
    return {"key": key, "value": value, "tokens": tokens, "count": len(kept),
            "budget": FACTS_TOKEN_BUDGET}


def drop_fact(store: Any, key: str) -> dict[str, Any]:
    """Remove one fact. A key nobody wrote is a fact about the store, not an error."""
    key = (key or "").strip()
    with store.lock():
        facts = read_facts(store)
        kept = [item for item in facts if item.get("key") != key]
        removed = len(kept) != len(facts)
        if removed:
            write_facts(store, kept)
    return {"key": key, "removed": removed, "count": len(kept),
            "tokens": facts_tokens(kept), "budget": FACTS_TOKEN_BUDGET}


def clear_facts(store: Any) -> dict[str, Any]:
    """Remove every fact. The writer's move: the anchor is a decision, not a click."""
    with store.lock():
        facts = read_facts(store)
        write_facts(store, [])
    return {"removed": len(facts), "count": 0, "tokens": 0, "budget": FACTS_TOKEN_BUDGET}
