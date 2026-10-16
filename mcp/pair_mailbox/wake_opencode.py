#!/usr/bin/env python3
"""Wake one recorded OpenCode session through the official HTTP API."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote


def service_endpoint() -> tuple[str, str]:
    """Return the running server URL and its password from the OpenCode CLI."""
    status = subprocess.run(["opencode", "service", "status"],
                            capture_output=True, text=True, timeout=10)
    url = status.stdout.strip().splitlines()[-1] if status.returncode == 0 and status.stdout.strip() else ""
    if not url.startswith("http"):
        return "", ""
    key = subprocess.run(["opencode", "service", "get", "password"],
                         capture_output=True, text=True, timeout=10)
    return url, (key.stdout.strip() if key.returncode == 0 else "")


def _get(url: str, auth: tuple[str, str]) -> dict:
    out = subprocess.run(["curl", "-s", "--max-time", "10", "-u",
                          f"{auth[0]}:{auth[1]}", url],
                         capture_output=True, text=True, timeout=15)
    try:
        return json.loads(out.stdout)
    except (ValueError, TypeError):
        return {}


def wake(project_root: str | os.PathLike, pointer: str,
         session_id: str | None) -> dict:
    """Wake the exact MCP-bound session; never choose by directory or recency."""
    if not isinstance(session_id, str) or not session_id.strip():
        return {"woken": False, "why": "no recorded OpenCode session id"}
    if len(session_id) > 256 or any(ord(char) < 32 for char in session_id):
        return {"woken": False, "why": "invalid recorded OpenCode session id"}
    url, password = service_endpoint()
    if not url or not password:
        return {"woken": False, "why": "no running opencode service"}
    auth = ("opencode", password)
    session_url = f"{url.rstrip('/')}/api/session/{quote(session_id, safe='')}"
    response = _get(session_url, auth)
    session = response.get("data")
    if not isinstance(session, dict) or session.get("id") != session_id:
        return {"woken": False, "why": "recorded OpenCode session is missing or stale"}
    directory = (session.get("location") or {}).get("directory")
    try:
        belongs = bool(directory) and Path(directory).resolve() == Path(project_root).resolve()
    except (OSError, TypeError, ValueError):
        belongs = False
    if not belongs:
        return {"woken": False, "why": "recorded OpenCode session belongs to another project"}

    payload = json.dumps({"parts": [{"type": "text", "text": pointer}], "text": pointer})
    out = subprocess.run(
        ["curl", "-s", "--max-time", "15", "-u", f"opencode:{password}",
         "-X", "POST", f"{url.rstrip('/')}/api/session/{quote(session_id, safe='')}/prompt",
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
