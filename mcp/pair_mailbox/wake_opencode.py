#!/usr/bin/env python3
"""Wake-адаптер OpenCode: инъекция хода по официальному HTTP API, fail-closed.

Гарантия — mailbox (§7), здесь только ускоритель: спящий агент с открытой
идл-сессией получает указатель как user-ход (delivery=steer) и сам зовёт
pair_read. Двусмысленность (0 сессий проекта, несколько свежих кандидатов,
401/404) = отказ: будить не того хуже, чем не будить вовсе.

Секрет не в коде: пароль читается `opencode service get password` в рантайме.
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
from server import utcnow  # noqa: E402 - после sys.path

IDLE_MAX_SECONDS = 30 * 60.0


def service_endpoint() -> tuple[str, str]:
    """(url, password) работающего фонового сервера, либо ("", "")."""
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
    """Свежие idle-сессии проекта, новейшая первой. Пустой список = двусмысленно."""
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
    """Инъекция одного указателя в свежую idle-сессию проекта. Fail-closed."""
    url, password = service_endpoint()
    if not url or not password:
        return {"woken": False, "why": "нет работающего opencode service"}
    fresh = candidate_sessions(str(project_root), url, ("opencode", password))
    if not fresh:
        return {"woken": False, "why": "нет свежих idle-сессий проекта"}
    if len(fresh) > 1:
        return {"woken": False, "why": f"{len(fresh)} кандидатов — двусмысленно, refuse"}
    payload = json.dumps({"parts": [{"text": pointer}], "text": pointer})
    out = subprocess.run(
        ["curl", "-s", "--max-time", "15", "-u", f"opencode:{password}",
         "-X", "POST", f"{url.rstrip('/')}/api/session/{fresh[0]}/prompt",
         "-H", "Content-Type: application/json", "-d", payload],
        capture_output=True, text=True, timeout=20)
    if '"type":"user"' in (out.stdout or ""):
        return {"woken": True, "why": "steer-ход принят", "session": fresh[0]}
    tag = (out.stdout or out.stderr or "")[:120]
    return {"woken": False, "why": f"API отказал: {tag}"}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: wake-opencode.py <project_root> <pointer>", file=sys.stderr)
        raise SystemExit(2)
    print(json.dumps(wake(sys.argv[1], sys.argv[2]), ensure_ascii=False))
