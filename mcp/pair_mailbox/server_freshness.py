"""Checking the MCP-server runtime freshness against the on-disk code.

Why: pair-mailbox servers live long (launching at tmux-session start). When
`mcp/pair_mailbox/*.py` files or other key modules update in the repo (say new
plan_gate / intent_gate / review_gate gates land), the server process keeps running
the old bytecode from memory. Past the commit every round then walks by the machine
gates (the round-63 incident, notes/finding.md).

The module moved out of `server.py` by the size rule: the monolith stands frozen
(scripts/file-size-baseline.tsv), with freshness logic isolated in its own component.
"""
from __future__ import annotations

import hashlib
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# The current Python process start time: set at module load
_PROCESS_START_TIME: float = time.time()
try:
    _proc_stat = Path(f"/proc/{os.getpid()}")
    if _proc_stat.is_dir():
        _PROCESS_START_TIME = _proc_stat.stat().st_mtime
except (OSError, ValueError):
    pass


def get_process_start_time() -> float:
    "Return the current-process start timestamp (epoch seconds)."
    return _PROCESS_START_TIME


def proc_available() -> bool:
    "Whether /proc exists: absent on macOS, where every /proc reader degrades."
    try:
        return Path("/proc").is_dir()
    except OSError:
        return False


def server_code_dir() -> Path:
    "The mcp/pair_mailbox server source dir."
    return Path(__file__).resolve().parent


def runtime_files(code_dir: Path | None = None) -> list[Path]:
    """The RUNTIME source files: every *.py except the test files.

    Test files are excluded, and this is the same FILE SET `pairrestart`
    (bin/pairrestart.runtime_code_mtime) uses — round 106 slice A's rider (b),
    measured as the cause of the 24h outage pattern: a commit touching only
    tests made every role's server stale, and `--yes` then killed them all.
    """
    target = code_dir if code_dir is not None else server_code_dir()
    try:
        return sorted(p for p in target.glob("*.py") if not p.name.startswith("test_"))
    except OSError:
        return []


def code_digest(code_dir: Path | None = None) -> tuple[str, dict[str, str]]:
    """The CONTENT digest of the runtime code.

    One digest for the whole set, and a per-file digest so a changed file can
    be named. Content, not mtime: a merge or a checkout rewrites the files
    with the same bytes, and the old mtime rule called that a stale server —
    round 138 measured it twice, both times at a round's close.
    """
    whole = hashlib.sha256()
    per_file: dict[str, str] = {}
    for path in runtime_files(code_dir):
        try:
            data = path.read_bytes()
        except OSError:
            continue
        digest = hashlib.sha256(data).hexdigest()
        per_file[path.name] = digest
        whole.update(path.name.encode("utf-8"))
        whole.update(b"\0")
        whole.update(digest.encode("utf-8"))
        whole.update(b"\0")
    return whole.hexdigest(), per_file


def snapshot_code(code_dir: Path | None = None) -> dict[str, Any]:
    """What a process would load right now: the content digest of the runtime code."""
    whole, per_file = code_digest(code_dir)
    return {"digest": whole, "files": per_file}


def changed_since(snapshot: dict[str, Any], code_dir: Path | None = None) -> list[str]:
    """The runtime files whose CONTENT differs from the snapshot.

    A file that vanished counts too: its code is still in memory.
    """
    _, per_file = code_digest(code_dir)
    known = snapshot.get("files") or {}
    changed = [name for name, digest in per_file.items() if known.get(name) != digest]
    changed += [name for name in known if name not in per_file and name not in changed]
    return sorted(changed)


# What this process loaded: the content digest of the runtime code at import
# time. The gate compares the disk against THIS, so a merge that rewrites the
# files with the same bytes leaves the server fresh, and an edit that keeps the
# old mtime still reads stale.
#
# The window this leaves open is the import itself: a file edited between its
# own import and this line would be read as fresh while the old bytecode runs.
# That window is the server's startup, milliseconds wide, and an agent edits
# between rounds, not during a startup.
_START_SNAPSHOT: dict[str, Any] = snapshot_code()


def latest_code_mtime(code_dir: Path | None = None) -> tuple[float, str]:
    """Find the max mtime across the server-dir RUNTIME source files.

    The same FILE SET as `pairrestart` (bin/pairrestart.runtime_code_mtime)
    uses — round 106 slice A's rider (b), measured as the cause of the 24h
    outage pattern: a commit touching only tests made every role's server
    stale, and `--yes` then killed them all.

    This is the mtime, kept for the report and for the cross-process callers
    (the doctor, pairrestart) that cannot read a process's own snapshot. The
    gate itself compares CONTENT: a merge rewrites the mtimes of files whose
    bytes did not change, and that is not a stale server.
    """
    target_dir = code_dir if code_dir is not None else server_code_dir()
    newest_mtime = 0.0
    newest_file = ""
    try:
        for p in target_dir.glob("*.py"):
            if p.name.startswith("test_"):
                continue
            try:
                st = p.stat()
                if st.st_mtime > newest_mtime:
                    newest_mtime = st.st_mtime
                    newest_file = p.name
            except OSError:
                continue
    except OSError:
        pass
    return newest_mtime, newest_file


def list_newer_than_start(code_dir: Path | None = None,
                          process_start_ts: float | None = None,
                          tolerance: float = 2.0) -> list[str]:
    """The RUNTIME *.py files whose mtime runs past a process start.

    This is the CROSS-PROCESS proxy: a process that did not load the code (the
    doctor, pairrestart) cannot read another's snapshot, and asks the weaker
    question instead. It over-reports — a merge rewrites the mtimes of files
    whose bytes did not change — which is the harmless direction: a warning the
    gate does not repeat.

    Test files are excluded, and this is the same FILE SET `latest_code_mtime`
    and `pairrestart` apply: a commit touching only tests must not read as a
    stale server. Measured live on round 120 — editing test_origin_guard.py
    while the server ran made `pair_state` report server_fresh=false with
    server_stale_files=["test_origin_guard.py"], which gates pair_advance
    (server.py:2364) for a runtime that never touched.
    """
    start_ts = process_start_ts if process_start_ts is not None else get_process_start_time()
    target_dir = code_dir if code_dir is not None else server_code_dir()
    cutoff = start_ts + tolerance
    newer: list[str] = []
    try:
        for p in sorted(target_dir.glob("*.py")):
            if p.name.startswith("test_"):
                continue
            try:
                if p.stat().st_mtime > cutoff:
                    newer.append(p.name)
            except OSError:
                continue
    except OSError:
        pass
    return newer


def list_stale_files(code_dir: Path | None = None,
                     process_start_ts: float | None = None,
                     tolerance: float = 2.0,
                     snapshot: dict[str, Any] | None = None) -> list[str]:
    """The RUNTIME files whose CONTENT differs from what the process loaded.

    This is the gate's own question: does the code on disk differ from the
    code in memory? Content, not mtime — see `changed_since`.
    """
    snap = snapshot if snapshot is not None else _START_SNAPSHOT
    return changed_since(snap, code_dir)


def check_server_freshness(code_dir: Path | None = None,
                           process_start_ts: float | None = None,
                           snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    """Check whether the running server stands fresh against on-disk files.

    The comparison is by CONTENT against the snapshot this process took at
    import: the code in memory differs from the code on disk. The mtime is
    reported, not judged — a merge rewrites the mtimes of files whose bytes did
    not change, and calling that a stale server is what blocked round 137's
    close twice.
    """
    snap = snapshot if snapshot is not None else _START_SNAPSHOT
    start_ts = process_start_ts if process_start_ts is not None else get_process_start_time()
    newest_mtime, newest_file = latest_code_mtime(code_dir)
    stale_files = list_stale_files(code_dir, snapshot=snap)

    is_fresh = len(stale_files) == 0
    lag_seconds = max(0.0, newest_mtime - start_ts) if not is_fresh else 0.0

    start_iso = datetime.fromtimestamp(start_ts, tz=timezone.utc).isoformat()
    newest_iso = (datetime.fromtimestamp(newest_mtime, tz=timezone.utc).isoformat()
                  if newest_mtime > 0 else "")

    return {
        "fresh": is_fresh,
        "started_at": start_iso,
        "started_ts": start_ts,
        "newest_code_file": newest_file,
        "newest_code_at": newest_iso,
        "newest_code_ts": newest_mtime,
        "lag_seconds": lag_seconds,
        "stale_files": stale_files,
        "why": ("the server is fresh" if is_fresh else
                f"code {stale_files[0]} changed since the server start — the code on disk "
                f"differs from the code in memory ({start_iso}). Restart the MCP server.")
    }


def assert_server_fresh(reopening: bool = False, backward: bool = False) -> None:
    """The server-freshness gate for tool_pair_advance.

    Backward steps stay always allowed for emergency rescue, except reopening
    (done -> implement), where launching a new round on a stale server refuses.
    Any forward move blocks on a stale server.
    """
    if backward and not reopening:
        return
    status = check_server_freshness()
    if not status["fresh"]:
        from server import ProtocolError
        files = ", ".join(status["stale_files"]) or status["newest_code_file"]
        action = "by reopening the round" if reopening else "by moving the phase"
        raise ProtocolError(
            f"the server went stale: {status['why']}",
            f"Changed files: {files}. Restart the MCP server before {action}."
        )
