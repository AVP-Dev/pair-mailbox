"""Predicates and row classification for the CLI's /mcp picker."""
from __future__ import annotations


def picker_open(text: str) -> bool:
    lowered = text.lower()
    return "mcp" in lowered and ("server" in lowered or "search" in lowered)


def pair_row_visible(text: str) -> bool:
    return any("pair" in line.lower() for line in text.splitlines())


def pair_row_state(text: str) -> str:
    """Classify the pair row without mistaking a negative status for live."""
    rows = [line for line in text.splitlines() if "pair" in line.lower()]
    joined = " ".join(rows).lower()
    if not rows:
        return "absent"
    if "not connected" in joined or "disconnected" in joined:
        return "failed"
    if "connected" in joined:
        return "connected"
    if "failed" in joined or "error" in joined:
        return "failed"
    if "disabled" in joined:
        return "disabled"
    return "present"
