"""Budget ceiling (S2): loop protection for rounds on machine metrics.
Metrics: messages (send/deliver), advances, hours (wall-clock), restarts (.restarts-*).
Invariants: done and steps back never block, pair_send stays free. Reset: pairctl budget --reset.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BUDGET_FILE = "budget.json"
RESET_FILE = "budget-reset.json"

DEFAULT_BUDGET: dict[str, Any] = {
    "max_messages": 200,
    "max_advances": 10,
    "max_hours": 24.0,
    "max_restarts": 10,
}


def load_budget(state_dir: Path) -> dict[str, Any]:
    """Load the ceiling config from .pair/budget.json with defaults."""
    target = Path(state_dir) / BUDGET_FILE
    budget = dict(DEFAULT_BUDGET)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            for k in DEFAULT_BUDGET:
                if k in data and data[k] is not None:
                    try:
                        budget[k] = float(data[k]) if k == "max_hours" else int(data[k])
                    except (ValueError, TypeError):
                        pass
    except (OSError, json.JSONDecodeError):
        pass
    return budget


def _parse_ts(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None


def _find_round_start(transcript_path: Path, current_round: int) -> tuple[datetime | None, int]:
    """Find the round start time and offset (transcript line number).

    The current round start is the last `advance` move from `done` (reopen)
    or the first file event when no advance-from-done is found.
    """
    if not transcript_path.is_file():
        return None, 0

    lines: list[str] = []
    try:
        with transcript_path.open(encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return None, 0

    last_open_idx = -1
    last_open_ts: datetime | None = None

    for idx, line in enumerate(lines):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except Exception:
            continue

        if entry.get("kind") == "advance" and entry.get("previous") == "done":
            last_open_idx = idx
            last_open_ts = _parse_ts(entry.get("ts"))

    if last_open_idx != -1:
        return last_open_ts, last_open_idx

    # With no reopen from done, the round started at the first transcript record
    for idx, line in enumerate(lines):
        try:
            entry = json.loads(line)
            return _parse_ts(entry.get("ts")), 0
        except Exception:
            continue

    return None, 0


def collect_metrics(state_dir: Path, state: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """Count the actual machine-counted metrics of the current round.

    Counts the reset through budget-reset.json for the current round.
    """
    state_dir = Path(state_dir)
    current_round = int(state.get("round", 0))
    if now is None:
        now = datetime.now(timezone.utc)

    # The reset marker check
    reset_file = state_dir / RESET_FILE
    reset_ts: datetime | None = None
    if reset_file.is_file():
        try:
            data = json.loads(reset_file.read_text(encoding="utf-8"))
            if int(data.get("round", -1)) == current_round:
                reset_ts = _parse_ts(data.get("reset_at"))
        except (OSError, json.JSONDecodeError, ValueError, TypeError):
            pass

    transcript_path = state_dir / "transcript.jsonl"
    round_start_ts, round_start_idx = _find_round_start(transcript_path, current_round)

    effective_start_ts = round_start_ts
    if reset_ts is not None:
        if effective_start_ts is None or reset_ts > effective_start_ts:
            effective_start_ts = reset_ts

    messages_count = 0
    advances_count = 0

    if transcript_path.is_file():
        try:
            with transcript_path.open(encoding="utf-8") as f:
                for idx, line in enumerate(f):
                    if idx < round_start_idx and reset_ts is None:
                        continue
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except Exception:
                        continue

                    entry_ts = _parse_ts(entry.get("ts"))
                    if reset_ts is not None:
                        if entry_ts is not None and entry_ts < reset_ts:
                            continue

                    kind = entry.get("kind")
                    if kind in ("send", "deliver"):
                        messages_count += 1
                    elif kind == "advance":
                        # Never count the reopen move itself when it started the round
                        if idx != round_start_idx:
                            advances_count += 1
        except OSError:
            pass

    hours = 0.0
    if effective_start_ts is not None:
        delta = (now - effective_start_ts).total_seconds()
        hours = max(0.0, delta / 3600.0)

    # Pilot restarts (.restarts-*)
    restarts_count = 0
    start_epoch = effective_start_ts.timestamp() if effective_start_ts else 0.0

    search_dirs = [state_dir, state_dir / "pilot"]
    seen_files = set()
    for sdir in search_dirs:
        if not sdir.is_dir():
            continue
        for rpath in sdir.glob(".restarts-*"):
            real = rpath.resolve()
            if real in seen_files:
                continue
            seen_files.add(real)
            try:
                raw = rpath.read_text(encoding="utf-8").split()
                for item in raw:
                    try:
                        t = float(item)
                        if t >= start_epoch:
                            restarts_count += 1
                    except ValueError:
                        continue
            except OSError:
                continue

    return {
        "round": current_round,
        "messages": messages_count,
        "advances": advances_count,
        "hours": round(hours, 2),
        "restarts": restarts_count,
        "round_start": effective_start_ts.isoformat() if effective_start_ts else None,
        "reset_applied": reset_ts is not None,
    }


def check_budget(state_dir: Path, state: dict[str, Any], to_phase: str, now: datetime | None = None) -> tuple[bool, str, dict[str, Any]]:
    """Check whether the budget breaks on the move to to_phase.

    Returns (ok, reason, metrics).
    Invariants:
    - The move to 'done' is ALWAYS allowed.
    - Steps back are ALWAYS allowed (checked by caller code or by to_phase == backward).
    """
    metrics = collect_metrics(state_dir, state, now=now)
    budget = load_budget(state_dir)

    # The move to done stays out of the check
    if to_phase == "done":
        return True, "", metrics

    breaches = []
    if metrics["messages"] >= budget["max_messages"]:
        breaches.append(f"messages: {metrics['messages']}/{budget['max_messages']}")
    if metrics["advances"] >= budget["max_advances"]:
        breaches.append(f"advances: {metrics['advances']}/{budget['max_advances']}")
    if metrics["hours"] >= budget["max_hours"]:
        breaches.append(f"hours: {metrics['hours']:.1f}/{budget['max_hours']}h")
    if metrics["restarts"] >= budget["max_restarts"]:
        breaches.append(f"restarts: {metrics['restarts']}/{budget['max_restarts']}")

    if breaches:
        why = (
            f"round budget breached {metrics['round']}: {', '.join(breaches)}. "
            f"Forward motion stops against looping. "
            f"Closing into 'done' or steps back stay allowed. "
            f"Limit reset: `pairctl budget --reset`."
        )
        return False, why, metrics

    return True, "", metrics


def reset_budget(state_dir: Path, round_number: int) -> None:
    """Reset the current round budget counters, writing a marker."""
    state_dir = Path(state_dir)
    target = state_dir / RESET_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "round": round_number,
        "reset_at": datetime.now(timezone.utc).isoformat(),
    }
    target.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def cmd_budget(store: Any, args: Any) -> int:
    """CLI handler: `pairctl budget [--reset] [--json]`."""
    state = store.read()
    current_round = int(state.get("round", 0))
    if getattr(args, "reset", False):
        reset_budget(store.root, current_round)
        store.append_transcript("budget_reset", "human", round=current_round)
        print(f"round budget {current_round} reset.")
        return 0

    metrics = collect_metrics(store.root, state)
    budget = load_budget(store.root)

    if getattr(args, "json", False):
        import json
        out = {
            "round": current_round,
            "metrics": metrics,
            "budget": budget,
            "ok": all([
                metrics["messages"] < budget["max_messages"],
                metrics["advances"] < budget["max_advances"],
                metrics["hours"] < budget["max_hours"],
                metrics["restarts"] < budget["max_restarts"],
            ]),
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    print(f"Round budget {current_round}:")
    print(f"  messages: {metrics['messages']}/{budget['max_messages']}")
    print(f"  moves:      {metrics['advances']}/{budget['max_advances']}")
    print(f"  hours:      {metrics['hours']:.1f}/{budget['max_hours']}h")
    print(f"  restarts:   {metrics['restarts']}/{budget['max_restarts']}")
    if metrics.get("reset_applied"):
        print("  (reset marker active)")
    return 0
