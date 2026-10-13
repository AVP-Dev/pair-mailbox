"""Roster self-heal: pane recheck and dead-entry expiry (#19.1, #19.3).

`_register` in server.py runs before every tool but rechecked only the CALLER’s
pane: the env pane before the write (pane_env_rejected) and foreign panes only when
ours was missing (pane_foreign_dropped). While our own agent lived, neighbour
entries went untouched — a stale pane hung till a manual `pairctl roster --prune`.
This module is that recheck, moved out of the server: server owns mutate/lock,
panes owns the tmux verdicts, and here the "drop pane / delete entry" decision
runs on ready facts.

Two independent facts is the whole module principle, as in 19.2 and prune:
`os.kill(pid, 0)` proves a process exists, not slot ownership (pids recycle),
and `%N` renumbers. One fact means a false refusal or a phantom. So dropping
runs only on a dead pid AND a confirmed pane check, and entry deletion only on
no pane AND a dead pid AND quiet past TTL.

Fail-closed everywhere: tmux unreachable or a failed check keeps the entry. A
transient tmux failure never wipes data (lesson #10: pane_pin was wiped forever
after a transient failure). The human pin (pane_pin) is never touched: an
override beats any automation.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# How long a paneless dead entry stays quiet before expiring. 7 days: shorter
# kills context after a vacation, longer hoards junk. Manual `pairctl roster
# --prune` stays the instant cleanup, TTL the insurance against eternal junk.
# Env for tests, so no week-long waits.
ROLE_TTL_SECONDS = float(os.environ.get("PAIR_ROLE_TTL_SECONDS", "604800") or 604800)


def _age_seconds(last_seen: Any) -> float | None:
    """Seconds since last_seen. Unmeasurable — None, not 0."""
    if not last_seen or not isinstance(last_seen, str):
        return None
    try:
        return (datetime.now(timezone.utc)
                - datetime.fromisoformat(last_seen)).total_seconds()
    except (ValueError, TypeError):
        return None


def recheck_peers(
    roles: dict[str, Any],
    self_role: str,
    project_root: Path,
    *,
    pid_alive: Callable[[Any], bool],
    pane_exists: Callable[[Any], bool],
    belongs_to_project: Callable[..., tuple],
    same_pane: Callable[[Any, Any], bool],
) -> list[dict[str, Any]]:
    """Recheck neighbour recorded panes (#19.1). Returns events.

    Mutates entries in place (drop `pane`); the caller writes the events into
    the transcript and saves state. Event: {"kind", "role", "other_role",
    ...transcript fields}.
    """
    events: list[dict[str, Any]] = []
    living_panes = [
        info.get("pane") for name, info in roles.items()
        if name != self_role and info.get("pane")
        and isinstance(info.get("pid"), int) and pid_alive(info["pid"])
    ]
    for name, info in roles.items():
        if name == self_role:
            continue
        pane = info.get("pane")
        if not pane or info.get("pane_pin"):
            # Nothing to check or pinned by human — skip.
            continue
        pid = info.get("pid")
        if isinstance(pid, int) and pid > 0 and pid_alive(pid):
            # A live process re-registers itself. Healing another live entry means
            # taking the pane from a working agent.
            continue
        # Ghost entry: pane already dropped, but UID/session_id stayed. They forever
        # trigger pane_uid_collision on every live-role registration onto a proven
        # pane — an eternally lit warning equals no warning. Now cleaned with pane in
        # all three branches.
        try:
            if not pane_exists(pane):
                for field in ("pane", "pane_pin_why", "pane_uid", "pane_session_id"):
                    info.pop(field, None)
                events.append({"kind": "pane_stale_dropped", "role": self_role,
                               "other_role": name, "pane": pane,
                               "why": "pane missing in tmux, process dead"})
                continue
        except Exception:
            # Nothing to check with never means "dead address" (the pin UNKNOWN lesson).
            continue
        try:
            ours, why_not = belongs_to_project(pane, project_root)
        except Exception:
            continue
        if not ours:
            for field in ("pane", "pane_pin_why", "pane_uid", "pane_session_id"):
                info.pop(field, None)
            events.append({"kind": "pane_foreign_dropped", "role": self_role,
                           "other_role": name, "pane": pane, "why": why_not})
            continue
        if any(same_pane(held, pane) for held in living_panes if held):
            for field in ("pane", "pane_pin_why", "pane_uid", "pane_session_id"):
                info.pop(field, None)
            events.append({"kind": "pane_stale_dropped", "role": self_role,
                           "other_role": name, "pane": pane,
                           "why": "pane held by another live role"})
    return events


def expire_dead_roles(
    roles: dict[str, Any],
    writer: Any,
    *,
    pid_alive: Callable[[Any], bool],
    ttl_seconds: float = ROLE_TTL_SECONDS,
) -> list[dict[str, Any]]:
    """Delete paneless dead entries past the quiet TTL (#19.3).

    Deletion conditions, all at once: no pane; pid dead or missing; last_seen
    older than TTL; not the write-right holder. Returns {"kind":
    "role_expired", ...} events for the transcript.
    """
    events: list[dict[str, Any]] = []
    for name, info in list(roles.items()):
        if info.get("pane") or info.get("pane_pin"):
            continue
        pid = info.get("pid")
        if isinstance(pid, int) and pid > 0 and pid_alive(pid):
            continue
        if name == writer:
            # Write authority never evaporates on a timer (as in prune).
            continue
        age = _age_seconds(info.get("last_seen"))
        if age is None or age < ttl_seconds:
            # Unmeasurable age (old entries with no last_seen) — fail-closed.
            continue
        roles.pop(name, None)
        events.append({"kind": "role_expired", "role": name,
                       "pid": pid, "age_seconds": int(age),
                       "why": "paneless dead entry expired on TTL"})
    return events
