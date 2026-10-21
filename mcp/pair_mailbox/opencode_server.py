#!/usr/bin/env python3
"""The opencode server that holds THIS pair's connection.

A pair that runs OpenCode standalone (`bin/pair-duo`) talks to an opencode
server of its own: a random port and a password that live in that process's
environment. The shared background service (`opencode service status`) is a
DIFFERENT server, and asking the CLI for it finds nothing when the pair runs
standalone -- measured in round 141:

    opencode service status          -> stopped
    wake_opencode.service_endpoint() -> ('', '')
    wake_opencode.wake(...)          -> {'woken': False, 'why': 'no running opencode service'}

while the instance that actually holds the session answers -- with ITS
password, not the shared one.

So the server that owns the connection is found the only honest way: walk the
process tree up from the pair's own MCP server and look for the opencode
process that spawned it. Nothing here guesses: the port comes from the
process's own listening socket, the password from its own environment, and
both are read at call time and written nowhere.

The password never leaves the machine: it is not put into `state.json`, not
into a note, not into a commit. This module returns it to the caller and
forgets it.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

# The opencode server process is the one the TUI spawns to host MCP clients
# (`opencode serve --stdio --port 0`). The shared background service is the
# same binary with `--service`, and it is NOT the one holding a standalone
# pair's connection -- it serves every project on the box at once, so its
# "active session" belongs to whoever typed last. That difference is the whole
# reason this module exists, so it is decided by the command line, not by taste.
_SERVER_ARGS = ("serve",)
_SHARED_SERVICE_FLAG = "--service"


def _cmdline(pid: int) -> list[str]:
    """The process's argv, split on NUL. Empty when the process is gone."""
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]


def _environ(pid: int) -> dict[str, str]:
    """The process's environment. Empty when it cannot be read."""
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return {}
    out: dict[str, str] = {}
    for item in raw.split(b"\0"):
        key, sep, value = item.partition(b"=")
        if not sep:
            continue
        out[key.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
    return out


def _parent(pid: int) -> int:
    """The process's parent pid (0 when unknown)."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    # The comm field may contain spaces and parentheses, so the fields after it
    # are found by cutting at the LAST ')': state, then ppid.
    tail = raw.rpartition(")")[2].split()
    try:
        return int(tail[1])
    except (IndexError, ValueError):
        return 0


def _listen_ports(pid: int) -> list[int]:
    """The TCP ports this process listens on.

    From the process's own socket inodes, matched against the kernel's listen
    table: the port belongs to THIS process, not to some other server that
    happens to be running.
    """
    inodes: set[str] = set()
    try:
        entries = list(Path(f"/proc/{pid}/fd").iterdir())
    except OSError:
        return []
    for entry in entries:
        try:
            target = os.readlink(entry)
        except OSError:
            continue
        if target.startswith("socket:[") and target.endswith("]"):
            inodes.add(target[8:-1])
    if not inodes:
        return []
    ports: list[int] = []
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = Path(table).read_text(encoding="utf-8", errors="replace").splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 10 or fields[3] != "0A":
                continue  # 0A is TCP_LISTEN; anything else is not a listener
            if fields[9] not in inodes:
                continue
            try:
                port = int(fields[1].split(":")[1], 16)
            except (IndexError, ValueError):
                continue
            if port and port not in ports:
                ports.append(port)
    return sorted(ports)


def _is_opencode_server(tokens: list[str]) -> bool:
    """Whether this command line is the opencode server that hosts MCP clients."""
    names = [Path(part).name for part in tokens]
    if "opencode" not in names:
        return False
    if not any(name in _SERVER_ARGS for name in names):
        return False
    return _SHARED_SERVICE_FLAG not in names


def ancestors(pid: int, limit: int = 8) -> list[int]:
    """The process and its parents, nearest first."""
    out: list[int] = []
    current = pid
    for _ in range(limit):
        if current <= 1:
            break
        out.append(current)
        current = _parent(current)
    return out


def find_opencode_server(pid: int, pid_start: int | None = None) -> dict | None:
    """The opencode server that spawned `pid` (the pair's own MCP server).

    `pid_start` is the process's start time: a pid alone proves nothing,
    because pids are recycled, and a recycled pid would point the discovery at
    somebody else's process tree. When the start time does not match, nothing
    is found -- fail closed.

    Returns None when no such ancestor exists, and a dict with the server's
    pid, its password and its URLs otherwise.
    """
    if not isinstance(pid, int) or pid <= 0:
        return None
    if pid_start is not None:
        from liveness import proc_starttime

        actual = proc_starttime(pid)
        if actual is None or int(actual) != int(pid_start):
            return None
    for candidate in ancestors(pid):
        tokens = _cmdline(candidate)
        if not _is_opencode_server(tokens):
            continue
        password = _environ(candidate).get("OPENCODE_PASSWORD", "")
        if not password:
            continue
        ports = _listen_ports(candidate)
        if not ports:
            continue
        return {"pid": candidate, "password": password,
                "urls": [f"http://127.0.0.1:{port}" for port in ports]}
    return None


def http_get(url: str, password: str, timeout: int = 10) -> dict:
    """GET a URL with the opencode server's basic auth. Never raises."""
    try:
        out = subprocess.run(["curl", "-s", "--max-time", str(timeout), "-u",
                              f"opencode:{password}", url],
                             capture_output=True, text=True, timeout=timeout + 5)
    except (OSError, subprocess.SubprocessError):
        return {}
    try:
        parsed = json.loads(out.stdout)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def active_session(url: str, password: str) -> dict | None:
    """The session the server is currently serving, or None.

    `/api/session/active` is the server's own answer to "which session are
    you on". For a standalone instance that is the TUI's session -- the one
    holding our MCP connection -- and not a guess by directory or recency.
    """
    response = http_get(f"{url.rstrip('/')}/api/session/active", password)
    data = response.get("data")
    return data if isinstance(data, dict) else None


def session_of_project(session: dict, project_root: str | os.PathLike) -> bool:
    """Whether this session belongs to the project we are asking about."""
    directory = (session.get("location") or {}).get("directory")
    try:
        return bool(directory) and Path(directory).resolve() == Path(project_root).resolve()
    except (OSError, TypeError, ValueError):
        return False


def bind_session(project_root: str | os.PathLike, pid: int | None = None,
                 pid_start: int | None = None) -> str:
    """The id of the session our own opencode server is serving, or "".

    The binding is exact: the session comes from the server that owns the
    connection, and it is accepted only when it belongs to this project. An
    empty answer means "no binding" -- never a guess.
    """
    found = find_opencode_server(os.getpid() if pid is None else pid, pid_start)
    if not found:
        return ""
    for url in found["urls"]:
        session = active_session(url, found["password"])
        if not session:
            continue
        if not session_of_project(session, project_root):
            continue
        session_id = session.get("id")
        if isinstance(session_id, str) and session_id.strip():
            return session_id
    return ""
