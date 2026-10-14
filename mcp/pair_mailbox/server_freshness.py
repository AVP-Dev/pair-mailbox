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


def latest_code_mtime(code_dir: Path | None = None) -> tuple[float, str]:
    """Find the max mtime across the server-dir *.py source files.

    Returns (max_mtime, path_of_newest_file).
    """
    target_dir = code_dir if code_dir is not None else server_code_dir()
    newest_mtime = 0.0
    newest_file = ""
    try:
        for p in target_dir.glob("*.py"):
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


def list_stale_files(code_dir: Path | None = None,
                     process_start_ts: float | None = None,
                     tolerance: float = 2.0) -> list[str]:
    "Return the *.py file names with mtime past the process start, tolerance in."
    start_ts = process_start_ts if process_start_ts is not None else get_process_start_time()
    target_dir = code_dir if code_dir is not None else server_code_dir()
    cutoff = start_ts + tolerance
    stale: list[str] = []
    try:
        for p in sorted(target_dir.glob("*.py")):
            try:
                if p.stat().st_mtime > cutoff:
                    stale.append(p.name)
            except OSError:
                continue
    except OSError:
        pass
    return stale


def check_server_freshness(code_dir: Path | None = None,
                           process_start_ts: float | None = None) -> dict[str, Any]:
    """Check whether the running server stands fresh against on-disk files.

    When the source-file mtime runs past the process start by the tolerance margin
    (say > 2 seconds), the server counts stale.
    """
    start_ts = process_start_ts if process_start_ts is not None else get_process_start_time()
    newest_mtime, newest_file = latest_code_mtime(code_dir)
    stale_files = list_stale_files(code_dir, start_ts)
    
    # A 2.0-second tolerance for filesystem skew at concurrent launch
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
                f"code {newest_file} changed {lag_seconds:.1f}s past the server start ({start_iso}) — "
                f"an old version runs in memory. Restart the MCP server.")
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
