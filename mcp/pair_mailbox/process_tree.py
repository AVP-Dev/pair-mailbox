"""Portable, read-only process tree discovery for the pair supervisor."""
from __future__ import annotations

import os
import platform
import ctypes
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


def process_info(pid: int) -> dict | None:
    """Return a platform-native process identity (including full argv)."""
    try:
        from launch_context import process_info as read_process_info
        return read_process_info(pid)
    except (ImportError, OSError, ValueError):
        return None


def process_start_time(pid: int) -> float | None:
    """Process start as epoch seconds on Linux and macOS."""
    try:
        proc = Path(f"/proc/{pid}")
        if proc.is_dir():
            return proc.stat().st_mtime
    except (OSError, ValueError):
        pass
    if platform.system() == "Darwin":
        info = process_info(pid)
        try:
            # launch_context uses libproc's pbi_start_tvsec.usec on macOS.
            return float(info["start"]) if info else None
        except (KeyError, TypeError, ValueError):
            return None
    return None


def process_ids() -> list[int]:
    """Enumerate host processes, failing visibly if the OS query fails."""
    if platform.system() == "Linux":
        try:
            return [int(name) for name in os.listdir("/proc") if name.isdigit()]
        except OSError as exc:
            raise RuntimeError(f"cannot enumerate /proc: {exc}") from exc
    if platform.system() == "Darwin":
        try:
            return _darwin_process_ids()
        except (OSError, RuntimeError, ValueError) as exc:
            raise RuntimeError(f"cannot enumerate processes with libproc: {exc}") from exc
    raise RuntimeError(f"process discovery unsupported on {platform.system()}")


def _darwin_process_ids() -> list[int]:
    """Enumerate PIDs with libproc; ps is commonly denied by CLI sandboxes."""
    libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    proc_listpids = libproc.proc_listpids
    proc_listpids.argtypes = [ctypes.c_uint32, ctypes.c_uint32,
                              ctypes.c_void_p, ctypes.c_int]
    proc_listpids.restype = ctypes.c_int
    capacity = 4096
    while True:
        buffer = (ctypes.c_int * capacity)()
        written = proc_listpids(1, 0, buffer, ctypes.sizeof(buffer))  # PROC_ALL_PIDS
        if written <= 0:
            errno = ctypes.get_errno()
            reason = os.strerror(errno) if errno else "returned no process data"
            raise RuntimeError(reason)
        if written < ctypes.sizeof(buffer):
            return [int(pid) for pid in buffer[:written // ctypes.sizeof(ctypes.c_int)]
                    if pid > 0]
        capacity *= 2


def pair_mailbox_servers(state_dir: Path, role: str | None = None) -> list[dict]:
    """Find only pair MCP servers whose explicit --state belongs to this project."""
    wanted = state_dir.resolve()
    found = []
    for pid in process_ids():
        info = process_info(pid)
        argv = info.get("argv", []) if info else []
        if not argv or not any(Path(arg).name == "server.py" for arg in argv):
            continue
        try:
            actual_role = argv[argv.index("--role") + 1]
        except (ValueError, IndexError):
            continue
        if role is not None and actual_role != role:
            continue
        try:
            raw_state = argv[argv.index("--state") + 1]
        except (ValueError, IndexError):
            raw_state = next((arg.split("=", 1)[1] for arg in argv
                              if arg.startswith("--state=")), "")
        if not raw_state:
            continue
        try:
            if Path(raw_state).resolve() != wanted:
                continue
        except (OSError, RuntimeError):
            continue
        found.append({"pid": pid, "role": actual_role,
                      "start": process_start_time(pid)})
    return found


def _parent_map() -> dict[int, list[int]]:
    children: dict[int, list[int]] = {}
    if platform.system() == "Darwin":
        try:
            pids = process_ids()
        except RuntimeError:
            return children
        for pid in pids:
            try:
                info = process_info(pid)
                ppid = int(info["ppid"]) if info else 0
            except (KeyError, TypeError, ValueError):
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
