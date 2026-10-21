#!/usr/bin/env python3
"""Wake one recorded OpenCode session through the official HTTP API.

The session that holds the pair's MCP connection lives on the opencode server
that spawned the pair's own server -- for a standalone pair that is a server of
its own, with a random port and a password in its environment. The shared
background service is a DIFFERENT server, and asking the CLI for it finds
nothing when the pair runs standalone (measured, round 141).

So the endpoint comes from the process tree (`opencode_server`), and the
session comes from that same server: its active session is the one it is
serving, which for a standalone instance is the TUI's session. The recorded id
stays as the fallback -- the old rule, for the shared service and for a server
that does not expose the active session.

Either way the answer is checked against this project's directory before
anything is typed, and every refusal keeps its own cause.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

import opencode_server


def _shared_endpoint() -> tuple[str, str]:
    """The shared background service's URL and password, from the OpenCode CLI."""
    status = subprocess.run(["opencode", "service", "status"],
                            capture_output=True, text=True, timeout=10)
    url = status.stdout.strip().splitlines()[-1] if status.returncode == 0 and status.stdout.strip() else ""
    if not url.startswith("http"):
        return "", ""
    key = subprocess.run(["opencode", "service", "get", "password"],
                         capture_output=True, text=True, timeout=10)
    return url, (key.stdout.strip() if key.returncode == 0 else "")


def _answers(url: str, password: str) -> bool:
    """Whether this URL is an opencode server that answers with its password."""
    try:
        out = subprocess.run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                              "--max-time", "10", "-u", f"opencode:{password}", url],
                             capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return False
    return (out.stdout or "").strip() == "200"


def _own_endpoint(found: dict) -> tuple[str, str]:
    """The URL and password of the opencode server we found in the tree.

    A server may hold more than one listening socket, so the URL that
    answers with this server's password is the one that is used; when none
    answers, the first is returned and the failure is reported by the call
    that asks it something.
    """
    for url in found["urls"]:
        if _answers(f"{url.rstrip('/')}/api/session/active", found["password"]):
            return url, found["password"]
    return found["urls"][0], found["password"]


def service_endpoint(role_pid: int | None = None,
                     role_pid_start: int | None = None) -> tuple[str, str]:
    """The endpoint of the opencode server that holds THIS pair's connection.

    The server is found by walking up from the pair's own MCP server process,
    so the answer belongs to this project and not to whoever else runs
    opencode on the box. The shared background service is the fallback, and
    only when no such ancestor exists.
    """
    if role_pid:
        found = opencode_server.find_opencode_server(role_pid, role_pid_start)
        if found:
            return _own_endpoint(found)
    return _shared_endpoint()


def _get(url: str, auth: tuple[str, str]) -> dict:
    """GET with basic auth. The shared service and the standalone server use
    the same call shape; only the endpoint differs."""
    return opencode_server.http_get(url, auth[1])


def _recorded_session(url: str, password: str, project_root: str | os.PathLike,
                      session_id: str | None) -> tuple[str, str]:
    """The recorded session, when it exists and belongs to this project.

    Returns (session_id, reason): the reason is empty on success and names
    the refusal otherwise.
    """
    if not isinstance(session_id, str) or not session_id.strip():
        return "", "no recorded OpenCode session id"
    if len(session_id) > 256 or any(ord(char) < 32 for char in session_id):
        return "", "invalid recorded OpenCode session id"
    session_url = f"{url.rstrip('/')}/api/session/{quote(session_id, safe='')}"
    response = _get(session_url, ("opencode", password))
    session = response.get("data")
    if not isinstance(session, dict) or session.get("id") != session_id:
        return "", "recorded OpenCode session is missing or stale"
    if not opencode_server.session_of_project(session, project_root):
        return "", "recorded OpenCode session belongs to another project"
    return session_id, ""


def _active_session(url: str, password: str,
                    project_root: str | os.PathLike) -> tuple[str, str]:
    """The session the server is currently serving, when it is ours.

    The server's own answer to "which session are you on" -- for a standalone
    instance that is the TUI's session, the one holding our MCP connection.
    It is not a guess by directory or by recency: it comes from the server
    that owns the connection, and it is accepted only when the session's own
    directory is this project.
    """
    session = opencode_server.active_session(url, password)
    if not isinstance(session, dict):
        return "", "the opencode server reports no active session"
    session_id = session.get("id")
    if not isinstance(session_id, str) or not session_id.strip():
        return "", "the opencode server reports no active session"
    if not opencode_server.session_of_project(session, project_root):
        return "", "the opencode server's active session belongs to another project"
    return session_id, ""


def wake(project_root: str | os.PathLike, pointer: str,
         session_id: str | None, role_pid: int | None = None,
         role_pid_start: int | None = None) -> dict:
    """Wake the exact MCP-bound session; never choose by directory or recency."""
    own = opencode_server.find_opencode_server(role_pid, role_pid_start) if role_pid else None
    if own:
        url, password = _own_endpoint(own)
        if not url or not password:
            return {"woken": False, "why": "no running opencode service"}
        target, reason = _active_session(url, password, project_root)
        if not target and reason == "the opencode server reports no active session":
            # The server does not expose its active session: the recorded id
            # is then the only name we have, and it is checked the same way.
            target, reason = _recorded_session(url, password, project_root, session_id)
    else:
        if not isinstance(session_id, str) or not session_id.strip():
            return {"woken": False, "why": "no recorded OpenCode session id"}
        if len(session_id) > 256 or any(ord(char) < 32 for char in session_id):
            return {"woken": False, "why": "invalid recorded OpenCode session id"}
        url, password = service_endpoint()
        if not url or not password:
            return {"woken": False, "why": "no running opencode service"}
        target, reason = _recorded_session(url, password, project_root, session_id)
    if not target:
        return {"woken": False, "why": reason}
    session_url = f"{url.rstrip('/')}/api/session/{quote(target, safe='')}"

    payload = json.dumps({"parts": [{"type": "text", "text": pointer}], "text": pointer})
    out = subprocess.run(
        ["curl", "-s", "--max-time", "15", "-u", f"opencode:{password}",
         "-X", "POST", f"{session_url}/prompt",
         "-H", "Content-Type: application/json", "-d", payload],
        capture_output=True, text=True, timeout=20)
    if '"type":"user"' in (out.stdout or ""):
        return {"woken": True, "why": "the steer turn accepted"}
    tag = (out.stdout or out.stderr or "")[:120]
    return {"woken": False, "why": f"the API refused: {tag}"}


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print("usage: wake-opencode.py <project_root> <session_id> <pointer>", file=sys.stderr)
        raise SystemExit(2)
    print(json.dumps(wake(sys.argv[1], sys.argv[3], sys.argv[2]), ensure_ascii=False))
