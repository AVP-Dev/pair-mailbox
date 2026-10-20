"""Reading and overview for the human: digest, mailbox, journal, notes, project list.

Moved out of `server.py` as its own responsibility: everything here only READS
and prints. No command of this module changes pair state — the only `state.json`
write belongs to `pairctl` in `pairctl_authority.py`.

The split is not stylistic. `server.py` held 725 lines of `pairctl` tangled with
protocol, and commands that read landed in one file with commands that write.
When the file is read to learn who writes now, it pulled in code that writes nothing.

What lives here and why together:

- `discover_projects` and `project_row` — "where pairs run at all", which
  `bin/pairchat` also leans on, unrelated to `pairctl`. Splitting them across
  two modules for the letter of the rule would force `pairchat` to depend on
  a CLI it never needs;
- `silence_seconds` and `human_age` — the only place where "silent" turns into
  human text. A second implementation of this arithmetic would drift from the
  first exactly as the report itself would.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from server import DIGEST_TOKEN_BUDGET, LOG_DEFAULT_LIMIT, Store, build_digest, estimate_tokens
from task_subject import subject_line


def cmd_digest(store: Store, args: argparse.Namespace) -> int:
    # The same text `pair_digest` renders, from one place: two digest
    # implementations drift apart, and the human sees what the agent never saw.
    text = build_digest(store, args.role, args.budget)
    sys.stdout.write(text)
    used = estimate_tokens(text)
    if used > args.budget:
        # A breached ceiling is a digest breakage, not a reason to pretend all is
        # well. Write to stderr and return a nonzero code.
        print(f"digest: {used} tokens at ceiling {args.budget}", file=sys.stderr)
        return 1
    return 0


def cmd_why(store: Store, args: argparse.Namespace) -> int:
    # Why whoever writes, writes now. A short question the agent must be able
    # to answer with a fact, not "let me look".
    rows = [project_row(root) for root in discover_projects(args.paths)]
    if args.json:
        print(json.dumps([{"project": row["project"], "writer": row["writer"],
                           "reason": row.get("reason", "")} for row in rows],
                         ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print("no project with pair state found")
        return 1
    for row in rows:
        print(f"{row['project']}: {row['writer'] or '—'} — {row.get('reason') or 'reason unnamed'}")
    return 0


def cmd_inbox(store: Store, args: argparse.Namespace) -> int:
    role = args.role
    inbox = store.mailbox_root / role
    if not inbox.is_dir():
        print(f"no inbox for {role!r} (they may not have started yet)")
        return 0
    files = sorted(inbox.glob("*.json"))
    print(f"{len(files)} message(s) waiting for {role}")
    for path in files:
        body = json.loads(path.read_text(encoding="utf-8"))
        print(f"\n--- #{body['seq']} {body['ts']} from {body['from']} ---")
        print(body["text"])
    return 0


def cmd_log(store: Store, args: argparse.Namespace) -> int:
    # A default limit instead of "all". Measured: without it the command dumped the
    # whole active journal into the terminal — half a megabyte and ~50k tokens per
    # round, even for a human who just looked at what happened. Those who need
    # the whole file ask for it explicitly.
    limit = 0 if args.all else args.limit
    shown = store.read_transcript(limit)
    for entry in shown:
        print(json.dumps(entry, ensure_ascii=False))
    if args.all:
        return 0
    total = store.transcript_entry_count()
    if total > len(shown):
        # Quietly serving 50 lines instead of 900 reads as the full journal.
        print(f"# showing the last {len(shown)} of {total}; rest: "
              f"--all or --limit N", file=sys.stderr)
    return 0


def cmd_all(store: Store, args: argparse.Namespace) -> int:
    # Deliberately ignores --state: "all" is exactly the command where one
    # state directory would defeat the point.
    rows = [project_row(root, args.stuck_min) for root in discover_projects(args.paths)]
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print("no project with pair state found")
        print("point the path:  pairctl all ~/work/foo")
        print("or say where to look:  PAIR_SCAN='~/work/*/.pair'")
        return 1
    print(render_table(rows))
    stuck = [row for row in rows if row["stuck"]]
    live = [row for row in rows if not row["closed"]]
    untouched = [row for row in rows if row["silence_seconds"] is None]
    print()
    print(f"{len(rows)} project(s), live {len(live)}, untouched {len(untouched)}")
    if stuck:
        names = ", ".join(row["project"] for row in stuck)
        print(f"silent past {args.stuck_min} min: {names}")
        print("  view:  PAIR_STATE=<path> bin/pairchat --once")
        return 3
    return 0


def cmd_notes(store: Store, args: argparse.Namespace) -> int:
    for path in sorted(store.notes_root.glob("*.md")):
        print(f"\n===== {path.name} =====")
        print(path.read_text(encoding="utf-8"))
    return 0


def extract_state(argv: list[str]) -> tuple[Path, list[str]]:
    """Pull --state out of argv wherever it appears and resolve it.

    Subparser defaults silently overwrite same-named top-level options in
    argparse, so the option is removed from the grammar entirely and handled
    here instead. Both `server.py --state X ctl status` and
    `server.py ctl status --state X` work.
    """
    # One resolver for all: explicit --state wins, then PAIR_STATE, then walk-up
    # to .pair/state.json (otherwise a server from a nested directory quietly
    # creates a second independent state — state-dir-not-pinned).
    # PAIR_STATE reads as one whole string, like the plugin: split would cut a
    # path with a space, and resolvers would diverge on exotica.
    raw = (os.environ.get("PAIR_STATE") or "").strip()
    explicit = ""
    rest: list[str] = []
    index = 0
    while index < len(argv):
        item = argv[index]
        if item == "--state" and index + 1 < len(argv):
            explicit = argv[index + 1]
            index += 2
            continue
        if item.startswith("--state="):
            explicit = item.split("=", 1)[1]
            index += 1
            continue
        rest.append(item)
        index += 1
    # Round 127: the rule lives in one place (`state_resolve`), so the shell
    # entry points and this one cannot drift apart again.
    from state_resolve import resolve_state_dir
    resolved, _reason = resolve_state_dir(explicit=explicit or None)
    return resolved, rest


def extract_pane(argv: list[str]) -> tuple[str, list[str]]:
    """Pull --pane out of argv wherever it appears or fallback to PAIR_PANE.

    Explicit channel for the running pane, avoiding reliance on inherited TMUX_PANE.
    """
    raw = (os.environ.get("PAIR_PANE") or "").strip()
    rest: list[str] = []
    index = 0
    while index < len(argv):
        item = argv[index]
        if item == "--pane" and index + 1 < len(argv):
            raw = argv[index + 1]
            index += 2
            continue
        if item.startswith("--pane="):
            raw = item.split("=", 1)[1]
            index += 1
            continue
        rest.append(item)
        index += 1
    return raw.strip(), rest


def walk_up_to_state(start: Path | None = None) -> Path:
    """Nearest .pair up from cwd — the same walk-up as the plugin.

    Without it a launch from a project subdir creates a second independent
    state while the plugin shows the root one: two opinions about the writer
    under one guard that exists exactly against that.

    Round 127: the walk-up itself now lives in `state_resolve` — one rule for
    every entry point, the shell ones included. This stays as the name the
    rest of the repo already calls.
    """
    from state_resolve import walk_up_state
    found = walk_up_state(start)
    if found is not None:
        return found
    return (start or Path.cwd()).resolve() / ".pair"


# Where discover_projects looks when the human did not say. PAIR_SCAN overrides it
# with a colon-separated list of globs, so "where should I look" is always something
# the human can answer rather than something we guess at.
DEFAULT_SCAN_GLOBS = ("~/work/*/.pair",)


def discover_projects(extra: list[str] | None = None) -> list[Path]:
    """Every project that actually has pair state, sorted by path.

    A directory only counts when state.json is really inside it. An empty or
    half-created .pair is not a project, and listing it would put a phantom row
    in the human's overview of what is running.

    Naming projects replaces the built-in search. Otherwise `pairchat --all
    ~/work/blog` would show the five projects the scan always finds plus blog,
    and the human who named one project would get six.
    """
    import glob as _glob

    raw = os.environ.get("PAIR_SCAN", "").strip()
    if raw:
        patterns = [p.strip() for p in raw.split(os.pathsep) if p.strip()]
    elif extra:
        patterns = []
    else:
        patterns = list(DEFAULT_SCAN_GLOBS)

    found: dict[str, Path] = {}

    def offer(candidate: Path) -> None:
        candidate = candidate.expanduser()
        # Accept either the .pair directory or the project that contains it, because
        # "pairctl all ~/work/test" is what a human will type.
        if candidate.is_dir() and not (candidate / "state.json").is_file():
            candidate = candidate / ".pair"
        if (candidate / "state.json").is_file():
            resolved = candidate.resolve()
            found[str(resolved)] = resolved

    for pattern in patterns:
        expanded = str(Path(pattern).expanduser()) if pattern.startswith("~") else pattern
        for hit in _glob.glob(expanded, recursive=True):
            offer(Path(hit))
    for given in extra or []:
        offer(Path(given))
    # Never lose the project the human is standing in, whatever PAIR_SCAN says.
    offer(Path.cwd() / ".pair")

    return [found[key] for key in sorted(found)]


def silence_seconds(root: Path) -> float | None:
    """Seconds since the last recorded event, or None when nothing was recorded.

    Taken from the transcript, not from state.json's updated_at: a state write that
    changed nothing would otherwise dress up a stalled pair as busy.
    """
    last: dict | None = None
    try:
        text = (root / "transcript.jsonl").read_text(errors="replace")
    except OSError:
        return None
    for raw in text.splitlines():
        if not raw.strip():
            continue
        try:
            last = json.loads(raw)
        except Exception:
            continue
    if not last or not last.get("ts"):
        return None
    try:
        stamp = datetime.fromisoformat(str(last["ts"]))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - stamp).total_seconds())


def human_age(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 90:
        return f"{int(seconds)} with "
    if seconds < 5400:
        return f"{int(seconds // 60)} min"
    if seconds < 172800:
        return f"{int(seconds // 3600)} h"
    return f"{int(seconds // 86400)} d"


def project_row(root: Path, stuck_min: int = 30) -> dict:
    """One project's row for the human's overview. Never raises on a broken file."""
    try:
        state = json.loads((root / "state.json").read_text(encoding="utf-8"))
    except Exception:
        state = {}
    roles = state.get("roles") or {}
    writer = state.get("writer")
    age = silence_seconds(root)
    phase = state.get("phase") or "?"
    closed = bool(state.get("closed_at")) or phase == "done"
    return {
        "path": str(root),
        "project": root.parent.name or str(root),
        "task": subject_line(state),
        "phase": phase,
        "writer": writer,
        "reason": (state.get("writer_reason") or "").strip(),
        "mode": state.get("mode", "fix"),
        "peers": sorted(name for name in roles if name != writer),
        "roles": sorted(roles),
        "silence_seconds": age,
        "silence": human_age(age),
        "closed": closed,
        "stuck": age is not None and not closed and age > stuck_min * 60,
        # Round 106 slice C (D3): the human arriving cold needs the exact command,
        # not a hunt across sockets. Derivable statelessly from the resolved root,
        # the same shape pairup uses — no state file needed.
        "tmux_socket": tmux_socket(root.parent),
        "attach": (f"tmux -L {tmux_socket(root.parent)} attach -t "
                   f"{tmux_socket(root.parent)}"),
    }


def tmux_socket(project_root: Path) -> str:
    """`pair-<basename>-<hash8>`, hash over the RESOLVED root path.

    The same derivation pairup uses, so a printed line names the server the pair
    actually runs on. Never read from state.json.
    """
    import hashlib
    try:
        resolved = str(Path(project_root).resolve())
    except OSError:
        resolved = str(project_root)
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:8]
    return f"pair-{Path(resolved).name}-{digest}"


def render_table(rows: list[dict]) -> str:
    """Aligned plain text. Column widths come from the data, never from a guess."""
    header = ("project", "phase", "mode", "writer", "critic", "silence", "why", "task")
    table = [
        (
            row["project"],
            row["phase"],
            "AUDIT" if row.get("mode") == "audit" else "fix",
            row["writer"] or "—",
            ", ".join(row["peers"]) or "—",
            "not started" if row["silence_seconds"] is None
            else row["silence"] + ("  STUCK" if row["stuck"] else ""),
            (row["reason"] or "—")[:40],
            (row["task"] or "—")[:36],
        )
        for row in rows
    ]
    widths = [
        max([len(header[i])] + [len(record[i]) for record in table]) for i in range(len(header))
    ]
    lines = ["  ".join(header[i].ljust(widths[i]) for i in range(len(header))).rstrip()]
    lines.append("  ".join("-" * w for w in widths))
    lines.extend(
        "  ".join(record[i].ljust(widths[i]) for i in range(len(record))).rstrip()
        for record in table
    )
    return "\n".join(lines)