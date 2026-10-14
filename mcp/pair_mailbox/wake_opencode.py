#!/usr/bin/env python3
"""The OpenCode wake adapter: turn injection over the official HTTP API, fail-closed.

The guarantee is mailbox (section 7), here only an accelerator: a sleeping agent with
an open idle session takes the pointer as a user turn (delivery=steer), calling
pair_read alone. Ambiguity (0 project sessions, several fresh candidates, 401/404)
means refusal: waking the wrong one reads worse than waking none.

No secret in code: the password reads `opencode service get password` at runtime.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "mcp" / "pair_mailbox"))
from server import utcnow  # noqa: E402 - past sys.path

IDLE_MAX_SECONDS = 30 * 60.0


def service_endpoint() -> tuple[str, str]:
    "(url, password) of the running background server, or (\"\", \"\")."
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


def candidate_sessions(project_root: str, url: str, auth: tuple[str, str]) -> list[str]:
    "Fresh project idle sessions, newest first. An empty list reads ambiguous."
    data = _get(f"{url.rstrip('/')}/api/session", auth)
    now_ms = time.time() * 1000
    fresh = []
    for item in (data.get("data") or []):
        directory = (item.get("location") or {}).get("directory") or ""
        idle = (item.get("time") or {}).get("idle")
        if directory != project_root or not idle:
            continue
        if (now_ms - idle) / 1000 > IDLE_MAX_SECONDS:
            continue
        fresh.append((idle, item["id"]))
    return [sid for _ts, sid in sorted(fresh, reverse=True)]


def wake(project_root: str | os.PathLike, pointer: str) -> dict:
    "Injecting one pointer into a fresh project idle session. Fail-closed."
    url, password = service_endpoint()
    if not url or not password:
        return {"woken": False, "why": "no running opencode service"}
    fresh = candidate_sessions(str(project_root), url, ("opencode", password))
    if not fresh:
        return {"woken": False, "why": "no fresh project idle sessions"}
    if len(fresh) > 1:
        return {"woken": False, "why": f"{len(fresh)} candidates - ambiguous, refuse"}
    payload = json.dumps({"parts": [{"text": pointer}], "text": pointer})
    out = subprocess.run(
        ["curl", "-s", "--max-time", "15", "-u", f"opencode:{password}",
         "-X", "POST", f"{url.rstrip('/')}/api/session/{fresh[0]}/prompt",
         "-H", "Content-Type: application/json", "-d", payload],
        capture_output=True, text=True, timeout=20)
    if '"type":"user"' in (out.stdout or ""):
        return {"woken": True, "why": "the steer turn accepted", "session": fresh[0]}
    tag = (out.stdout or out.stderr or "")[:120]
    return {"woken": False, "why": f"the API refused: {tag}"}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: wake-opencode.py <project_root> <pointer>", file=sys.stderr)
        raise SystemExit(2)
    print(json.dumps(wake(sys.argv[1], sys.argv[2]), ensure_ascii=False))
