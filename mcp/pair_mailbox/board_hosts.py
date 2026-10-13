"""Liveness of what lives on this machine: tmux panes, pids, the supervisor process.

A separate module, because these are a different breed of facts than the protocol record.
`board_snapshot` reads what the pair recorded about itself: phase, roles, tasks, journal.
Here — what only the system answers when asked: is the process alive, is there a
pane, when did files change. Mixing them in one module would force the snapshot
test to hold a real tmux, while on a tmux-less machine the supervisor-liveness
check would pass without ever reaching `/proc`.

Honesty contract: "unknown" and "no" are different answers, and different they stay
on all three levels.

- panes: `pane_alive` may stay silent (no tmux, odd-shaped pane), and that is
  `None`, not `False`;
- pid: a missing record is `None`, not "process dead";
- supervisor: "process not found" means exactly what it says.

The module never writes or fixes: an observer, like the whole dashboard.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pairctl_read import human_age

PILOT_LOG = "pairpilot.log"
PILOT_REPORT = "REPORT.md"


def mtime_age(path: Path) -> float | None:
    """Seconds since the file changed. None when missing or unreachable."""
    try:
        stamp = datetime.fromtimestamp(Path(path).stat().st_mtime, timezone.utc)
    except OSError:
        return None
    return max(0.0, (datetime.now(timezone.utc) - stamp).total_seconds())


def pane_status(pane: Any) -> tuple[bool | None, str]:
    """Whether the role pane is alive. (None, reason) means "unknown", not "no".

    `composer` imports lazily: it pulls `os` and `re`, but at top level it would
    intercept the import for everyone, including a tmux-less snapshot build. When
    tmux is missing or fell, that is missing data, not a dead pane: a red dot on
    a live role would send the human fixing what never broke.
    """
    text = str(pane or "").strip()
    if not text:
        return None, "pane unrecorded"
    try:
        from composer import _tmux, pane_alive
    except Exception as error:  # noqa: BLE001 - the reason must stay visible in the feed
        return None, f"tmux unreachable: {error}"
    try:
        return (True if pane_alive(_tmux(), text) else False), ""
    except Exception as error:  # noqa: BLE001 - better to check the pane than to fall
        return None, str(error)


def resolve_pane_title(pane: Any) -> str:
    """The human pane name by its id: `%0` -> `mail:0.0`.

    Reuses `composer.pane_identity` instead of asking tmux separately, and that is
    no one-call saving but a correctness demand. Our own query of the same
    `display-message` returned code 0 and `":."` for a NONEXISTENT pane — that is,
    nonempty, hence "answer-looking" garbage, and the dashboard showed the phantom
    pane as `pane %999999 [.:]`. Exactly this deception class already cost the
    project two same-named functions with different rules, see `server.pane_exists`
    vs `composer.pane_alive`.
    """
    text = str(pane or "").strip()
    if not text.startswith("%"):
        return text
    try:
        from composer import _tmux, pane_identity
    except Exception as error:  # noqa: BLE001 - the pane name is decor, not a fact
        return text
    try:
        identity = pane_identity(_tmux(), text)
    except Exception:  # noqa: BLE001 - better to show %0 than to drop the build
        return text
    if not identity.get("exists"):
        return text
    address = str(identity.get("address") or "").strip()
    return address or text


def clean_pid(value: Any) -> int | None:
    """The real pid or None. Zero and garbage mean "unrecorded", not "dead".

    `server.is_pid_alive` answers False on small numbers and non-integers, and into
    the snapshot that would ride as "process dead" for a role whose pid nobody wrote.

    `bool` cuts explicitly: in Python `isinstance(True, int)` holds, and without
    this check `True` would pass as pid 1 — that is, as init.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def pilot_pids(script: str) -> list[int]:
    """All live processes whose command line holds `script`.

    A separate public function, not part of `pilot_section`: its substance is the
    `/proc` walk, checked apart — otherwise the supervisor-liveness test would pass
    without ever reaching the walk. Our own pid skips — the caller would find
    itself and declare itself supervisor, the lie kind worse than missing data.

    The supervisor never writes its pid into state — it owns the process, and a
    separate "pilot alive" record would go stale right after the fall. The search
    walks all of `/proc/<pid>/cmdline`, because the script name there may run
    relative: exactly how `finish.sh` launches it.
    """
    found: list[int] = []
    try:
        names = os.listdir("/proc")
    except OSError:
        return found
    for name in names:
        if not name.isdigit() or int(name) == os.getpid():
            continue
        try:
            raw = Path("/proc", name, "cmdline").read_bytes()
        except OSError:
            continue
        if script.encode() in raw:
            found.append(int(name))
    return sorted(found)


def pilot_section(state_dir: Path, entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Whether the supervisor lives and what it does.

    Looks for EXACTLY our `bin/pairpilot` in EXACTLY this project: another pilot
    from the neighbouring work copy must never glow green here.

    The file age renders twice: in seconds (thresholds are the client call) and in
    words (for show). Second-to-text formatting is one implementation per project,
    `pairctl_read.human_age`, and a second copy in JavaScript would drift from it
    on the very first threshold change.
    """
    state_dir = Path(state_dir)
    log_path = state_dir / PILOT_LOG
    report_path = state_dir / PILOT_REPORT
    pids = pilot_pids(str(state_dir.parent / "bin" / "pairpilot"))
    log_age = mtime_age(log_path)
    report_age = mtime_age(report_path)

    pilot_events = [e for e in entries if str(e.get("kind", "")).startswith("pilot_")]
    refusals = sum(1 for e in pilot_events
                   if "refus" in str(e.get("kind")) or "impossible" in str(e.get("kind")))
    return {
        "running": bool(pids),
        "pid": min(pids) if pids else None,
        "pids": pids,
        "log_path": str(log_path),
        "log_size": log_path.stat().st_size if log_path.exists() else 0,
        "log_age_seconds": log_age,
        "log_age": human_age(log_age),
        "report_path": str(report_path),
        "report_exists": report_path.exists(),
        "report_age_seconds": report_age,
        "report_age": human_age(report_age),
        "events_shown": len(pilot_events),
        "refusals": refusals,
        "last": _last_summary(pilot_events),
        "last_ts": pilot_events[-1].get("ts") if pilot_events else None,
    }


def _last_summary(entries: list[dict[str, Any]]) -> str:
    """What the pilot did last. Our own one-line pick instead of importing
    `summarise_event`: that one lives in the snapshot and is about journal text,
    while here only the "there was a cause" fact matters — the event feed below
    shows the details anyway."""
    if not entries:
        return ""
    for field in ("why", "prompt", "value", "phase", "path"):
        if entries[-1].get(field) not in (None, "", {}, []):
            return str(entries[-1][field]).strip().replace("\n", " ")[:160]
    return str(entries[-1].get("kind") or "")