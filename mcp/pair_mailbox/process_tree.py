"""Portable, read-only process tree discovery for the pair supervisor."""
from __future__ import annotations

import os
import platform
import subprocess
from pathlib import Path


def cmdline(pid: int) -> list[str]:
    if platform.system() == "Darwin":
        try:
            from launch_context import process_info
            info = process_info(pid)
            return info.get("argv", []) if info else []
        except (ImportError, OSError, ValueError):
            return []
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]


def _parent_map() -> dict[int, list[int]]:
    children: dict[int, list[int]] = {}
    if platform.system() == "Darwin":
        try:
            result = subprocess.run(["/bin/ps", "-axo", "pid=,ppid="],
                                    capture_output=True, text=True, timeout=3)
        except (OSError, subprocess.SubprocessError):
            return children
        if result.returncode != 0:
            return children
        rows = (line.split() for line in result.stdout.splitlines())
        for fields in rows:
            if len(fields) != 2:
                continue
            try:
                pid, ppid = map(int, fields)
            except ValueError:
                continue
            children.setdefault(ppid, []).append(pid)
    elif platform.system() == "Linux":
        try:
            entries = os.listdir("/proc")
        except OSError:
            return children
        for entry in entries:
            if not entry.isdigit():
                continue
            try:
                stat = Path(f"/proc/{entry}/stat").read_text()
                ppid = int(stat.rsplit(")", 1)[1].split()[1])
            except (OSError, IndexError, ValueError):
                continue
            children.setdefault(ppid, []).append(int(entry))
    return children


def descendants(root: int) -> list[int]:
    """Return descendants breadth-first so the nearest role process wins."""
    children = _parent_map()
    out: list[int] = []
    frontier = [root]
    while frontier:
        current = frontier.pop(0)
        for child in children.get(current, []):
            out.append(child)
            frontier.append(child)
    return out
