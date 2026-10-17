"""The pair-state snapshot for the static dashboard: data only, not a byte of HTML.

Why a separate layer. The dashboard opens on a double click, so the page owns no
server and no right to read `.pair/` itself: from `file://` the browser bans
`fetch()` of local files. The only honest data source is the process that read
state and inlined it into the page; `board_render.py` only turns its output into
a page.

Why the "silent" arithmetic lives here, not in `pairctl_read.silence_seconds`. That
one parses the whole `transcript.jsonl`, having no tail at hand. Here the tail is
already read (`Store.read_transcript(limit)` reads from the file end: 67.5 ms ->
0.4 ms on a 2 MB file). Second-to-text formatting stays single: `human_age`.

About "alive" — in `board_hosts.py`: those are system questions, not protocol
ones, and keeping them here would test the snapshot only with tmux around.
Nothing writes: the dashboard observes, and the only file ever appearing is the
HTML itself, written by `bin/pairboard`.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from board_hosts import (clean_pid, mtime_age, pane_status, pilot_section,
                         resolve_pane_title)
from pairctl_read import human_age
from server import PHASES, Store, is_pid_alive, utcnow
from task_subject import subject_line

# How many latest journal records land on the page. The journal grows on its own and
# on a long round that is hundreds of megabytes; the dashboard shows the picture, not the archive.
DEFAULT_EVENTS = 40

# After how many seconds without a rebuild the snapshot counts as stale: the page
# shows it with a mark instead of faking a lively picture.
DEFAULT_STALE_AFTER = 90

# Trimming long values. Causes in evidence and `why` run to paragraphs, and the dashboard
# has no place for them: the page must read, not scroll.
MAX_VALUE_CHARS = 160

TASK_TEXT_CHARS = 300


def _age(stamp: Any) -> float | None:
    """Seconds since the timestamp. None when nothing parses."""
    if not stamp:
        return None
    try:
        moment = datetime.fromisoformat(str(stamp))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - moment).total_seconds())


def _clip(value: Any, limit: int = MAX_VALUE_CHARS) -> str:
    text = str(value if value is not None else "").strip().replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def summarise_event(entry: dict[str, Any]) -> str:
    """One short line per event.

    The journal is mixed: `send` carries subject, `evidence` value, `advance` the
    phase. Picking the first meaningful field gives a readable feed where no record
    turns into a json dump. An unknown kind stays its own kind name: inventing a
    meaning for an event missing from the dictionary is banned.
    """
    for field in ("subject", "why", "value", "prompt", "reason", "text", "note",
                  "phase", "mode", "autonomy", "status", "path", "bell_kind",
                  "note_kind", "evidence_kind", "pane", "role"):
        if entry.get(field) not in (None, "", {}, []):
            return _clip(entry[field])
    return entry.get("kind", "?")


def _role_rows(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Roles: three liveness facts and silence in human units."""
    writer = state.get("writer")
    rows: list[dict[str, Any]] = []
    for name in sorted(state.get("roles") or {}):
        info = state.get("roles", {}).get(name) or {}
        pid = clean_pid(info.get("pid"))
        pid_alive = is_pid_alive(pid) if pid else False
        alive, why = pane_status(info.get("pane"))
        silence = _age(info.get("last_seen"))
        pane_str = str(info.get("pane") or "")
        pane_human = resolve_pane_title(pane_str) if pane_str else ""
        rows.append({
            "role": name,
            "is_writer": name == writer,
            "pane": pane_str,
            "pane_human": pane_human,
            "pane_alive": alive,
            "pane_note": why,
            "pid": pid,
            "pid_alive": pid_alive,
            "last_seen": info.get("last_seen"),
            "silence_seconds": silence,
            "silence": human_age(silence),
            "sends": int(info.get("sends") or 0),
            "reads": int(info.get("reads") or 0),
            "seen": int((state.get("seen") or {}).get(name) or 0),
            "restored_by": _clip(info.get("pane_restored_by")),
        })
    # The writer first: the one watched before all.
    rows.sort(key=lambda row: (not row["is_writer"], row["role"]))
    return rows


def _state_read_error(state_dir: Path) -> str:
    """Why the state never reads as state. An empty line means it reads.

    A separate check is needed because `Store.read()` never raises on a broken
    `state.json` but quietly swaps in defaults. For the protocol that is right: the
    server must never fall over a file. For the DASHBOARD it is a lie: the page
    would show a blank phase and zero roles, and the human would decide the pair
    lost its agents while the file merely broke. Cost — one pass over a small file,
    gain — truth on screen.
    """
    path = Path(state_dir) / "state.json"
    if not path.exists():
        return ""
    try:
        json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return f"state.json unreadable: {error}"
    return ""


def _task_rows(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Tasks: open first, inside from new to old."""
    rows = []
    for item in state.get("tasks") or []:
        rows.append({
            "id": item.get("id"),
            "text": _clip(item.get("text"), TASK_TEXT_CHARS),
            "status": str(item.get("status") or "open"),
            "round": item.get("round"),
            "added_by": item.get("added_by") or "",
            "added_at": item.get("added_at"),
            "closed_by": item.get("closed_by") or "",
            "closed_at": item.get("closed_at"),
            "reason": _clip(item.get("reason")),
        })
    rows.sort(key=lambda row: (row["status"] != "open", -(int(row["id"]) if
                        isinstance(row["id"], int) else 0)))
    return rows


def _discussion_rows(store: "Store", limit: int = 5) -> list[dict[str, Any]]:
    """The last discussion: letters with text, not pings.

    The mailbox lies by role, with no shared seq — order restores from `ts`.
    An empty subject on meaningful text is the ping-antipattern (§9a): never
    reaching the discussion, staying in the event feed.
    """
    letters: list[dict[str, Any]] = []
    root = store.mailbox_root
    try:
        paths = list(root.glob("*/*.json")) if root.is_dir() else []
    except OSError:
        return []
    for path in paths:
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        text = str(body.get("text") or "").strip()
        if not text:
            continue
        letters.append({
            "ts": body.get("ts"),
            "from": str(body.get("from") or ""),
            "to": str(body.get("to") or ""),
            "subject": _clip(body.get("subject")),
            "text": _clip(text, TASK_TEXT_CHARS),
        })
    letters.sort(key=lambda row: str(row["ts"] or ""))
    return letters[-limit:]


def _obligation_rows(store: "Store") -> list[dict[str, Any]]:
    """Open obligations: who waits on whom and with what question."""
    rows: list[dict[str, Any]] = []
    root = store.obligations_root
    try:
        paths = sorted(root.glob("*.json")) if root.is_dir() else []
    except OSError:
        return []
    for path in paths:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        rows.append({
            "seq": record.get("seq"),
            "from": str(record.get("from") or ""),
            "to": str(record.get("to") or ""),
            "subject": _clip(record.get("subject")),
        })
    return rows


def _event_rows(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The event feed: facts with no words about them, the dict lives in the client."""
    rows = []
    for entry in entries:
        # A broken journal line is an event of its own kind, not a skip:
        # skipping quietly would pretend the event never was.
        if entry.get("_damaged"):
            rows.append({"ts": None, "kind": "damaged", "role": "",
                         "to": "", "summary": _clip(entry.get("_raw"))})
            continue
        rows.append({
            "ts": entry.get("ts"),
            "kind": str(entry.get("kind") or "?"),
            "role": entry.get("role") or "",
            "to": entry.get("to") or "",
            "summary": summarise_event(entry),
        })
    return rows


def _round_section(state: dict[str, Any], event_age: float | None) -> dict[str, Any]:
    phase = str(state.get("phase") or "scoping")
    state_age = _age(state.get("updated_at"))
    return {
        "number": state.get("round", 0),
        "phase": phase,
        "phases": list(PHASES),
        "phase_index": PHASES.index(phase) + 1 if phase in PHASES else 0,
        "mode": state.get("mode") or "fix",
        "autonomy": state.get("autonomy") or "auto",
        "writer": state.get("writer") or "",
        "writer_reason": _clip(state.get("writer_reason")),
        # subject_line, not the raw scalar: round 103's pane read "Round 98: …"
        # while round 103 worked. A stale subject is the agent's first orientation
        # and the last place it belongs.
        "task": _clip(subject_line(state), TASK_TEXT_CHARS),
        "updated_at": state.get("updated_at"),
        "state_age_seconds": state_age,
        "state_age": human_age(state_age),
        "event_age_seconds": event_age,
        "event_age": human_age(event_age),
        "closed": phase == "done" or bool(state.get("closed_at")),
    }


def build_snapshot(state_dir: Path, *, limit: int = DEFAULT_EVENTS,
                   stale_after: int = DEFAULT_STALE_AFTER) -> dict[str, Any]:
    """The pair-state snapshot. Never throws on broken state.

    The dashboard opens when something broke, and "could not read state.json" is
    the worst possible answer: a blank screen with no cause instead of a picture.
    So every parse forgives with None, and the snapshot marks itself partial.
    """
    state_dir = Path(state_dir)
    store = Store(state_dir)
    read_error = _state_read_error(state_dir)
    try:
        state = store.read()
    except Exception as error:  # noqa: BLE001 - the reason shows, never swallowed
        state = store.default_state()
        read_error = read_error or str(error)

    entries = store.read_transcript(limit)
    try:
        total = store.transcript_entry_count()
    except Exception:  # noqa: BLE001 - the counter is decor, its lack is no reason to fall
        total = len(entries)

    tasks = _task_rows(state)
    event_age = _age(entries[-1].get("ts")) if entries else None
    try:
        from server_freshness import proc_available as _proc_available
        no_proc = not _proc_available()
    except Exception:
        no_proc = False
    return {
        "generated_at": utcnow(),
        "project": state_dir.parent.name or str(state_dir),
        "platform_no_proc": no_proc,
        "state_dir": str(state_dir),
        "read_error": read_error,
        "stale_after": int(stale_after),
        "watch": False,
        "round": _round_section(state, event_age),
        "roles": _role_rows(state),
        "waiting": _obligation_rows(store),
        "discussion": _discussion_rows(store),
        "pilot": pilot_section(state_dir, entries),
        "tasks": tasks,
        "events": _event_rows(entries),
        "counts": {"events_shown": len(entries), "events_total": total,
                   "tasks_open": sum(1 for row in tasks if row["status"] == "open"),
                   "roles": len(state.get("roles") or {})},
    }


def snapshot_json(snapshot: dict[str, Any]) -> str:
    """The snapshot as a string, safe to inline inside `<script>`.

    A closing tag in data breaks the page, and data here is letter and task text —
    exactly what the human wrote itself. So `<`, `>` and `&` leave into JSON
    escape sequences: the value after parsing stays the very same char, and the
    page stops being something closable. U+2028 and U+2029 are JS line separators
    but not JSON ones; naive inlining breaks the parser, and they escape too.
    """
    text = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
    # The backslash needs NO escaping: `json.dumps` already gave valid JSON.
    # Escaping it twice would turn every escaped quote into backslash-quote and break parsing.
    # Only escape sequences join that the source text never held, so replacement order
    # never matters.
    for needle, replacement in (("&", "\\u0026"), ("<", "\\u003c"), (">", "\\u003e"),
                                ("\u2028", "\\u2028"), ("\u2029", "\\u2029")):
        text = text.replace(needle, replacement)
    return text