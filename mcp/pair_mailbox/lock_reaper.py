"""Stale role locks: a held record with a dead process (round 108 slice 2).

The invariant this module exists for, named by the critic at seq 1623 and
measured false three times in one hour: **the lock file is authoritative for who
holds the role; pgrep is not authoritative for liveness.** A pane's word and a
process disagree, the process wins; and where even the process is ambiguous, the
kernel's own flock is the fact.

What slice D changed: a socket daemon can outlive its agent BY DESIGN, so "the
lock is held" no longer implies "an agent is working", and "the pid record is
gone" no longer implies "the role is free". Three signals, kept separate:

- the FLOCK (kernel fact): if `LOCK_EX|LOCK_NB` succeeds, the role is FREE,
  whatever any file or pid record says;
- the PID RECORD (`.server-<role>.pid`): a leftover bookkeeping file. Its pid
  being dead means the record is stale and must be reaped; its pid being alive
  means a live holder is managing the role and the record is kept;
- the PANE (operator's mental model): addressed by the pilot, never used as a
  liveness proof here.

`report()` returns the three-state answer the pilot's dead-pane path needs, and
`reap()` cleans the stale records so a role cannot be wedged by a corpse's file.
"""
from __future__ import annotations

import errno
import fcntl
import json
from pathlib import Path
from typing import Any


def lock_file(state_dir: Path, role: str) -> Path:
    return Path(state_dir) / f".server-{role}.lock"


def pid_file(state_dir: Path, role: str) -> Path:
    return Path(state_dir) / f".server-{role}.pid"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True      # alive, owned by someone else
    except OSError:
        return False
    return True


def flock_free(path: Path) -> bool:
    """The kernel's answer: can this lock be taken right now?"""
    if not path.exists():
        return True
    handle = path.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EAGAIN):
            return False
        return False
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
    return True


def _recorded_pid(path: Path) -> int:
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return 0
    try:
        return int(raw.splitlines()[0]) if raw else 0
    except ValueError:
        return 0


def role_state(state_dir: Path, role: str) -> str:
    """`live` | `free` | `stale` for one role.

    `live` — the flock is held (a process holds the role, daemon or not);
    `stale` — a pid record exists whose process is dead, or a lock file exists
      that no flock holds;
    `free` — nothing at all: no lock file, no pid record.
    """
    lock = lock_file(state_dir, role)
    pidf = pid_file(state_dir, role)
    if lock.exists() and not flock_free(lock):
        return "live"
    recorded = _recorded_pid(pidf) if pidf.exists() else 0
    if recorded and not _pid_alive(recorded):
        return "stale"
    if lock.exists() and not pidf.exists():
        # a bare lock file with no holder and no record: the kernel lets it be
        # taken, so it is not blocking anybody — but NAME it, because an
        # operator checking `ls` would conclude otherwise.
        return "free"
    if recorded and _pid_alive(recorded):
        return "live"
    return "free"


def report(state_dir: Path) -> dict[str, str]:
    """Every role known to this state, with its state. Never silent."""
    state_dir = Path(state_dir)
    try:
        data = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
        roles = list((data.get("roles") or {}) or {})
    except (OSError, ValueError):
        roles = []
    # plus any role that left a lock/pid file behind, even without a roster entry
    for path in state_dir.glob(".server-*.pid"):
        name = path.name[len(".server-"):-len(".pid")]
        if name not in roles:
            roles.append(name)
    return {role: role_state(state_dir, role) for role in sorted(set(roles))}


def reap(state_dir: Path) -> list[str]:
    """Clean stale records. Returns the roles cleaned.

    Only the RECORD is removed, and only for a dead pid: the kernel's flock is
    never forced (a live holder's lock is untouched by construction, because the
    pid record of a live holder names a live pid). A bare lock file with no
    holder is left alone — it blocks nobody, and removing it would hide from an
    operator which roles have ever run here.
    """
    cleaned: list[str] = []
    for path in sorted(state_dir.glob(".server-*.pid")):
        pid = _recorded_pid(path)
        if pid and not _pid_alive(pid):
            try:
                path.unlink()
                cleaned.append(path.name[len(".server-"):-len(".pid")])
            except OSError:
                continue
    return cleaned


import os  # noqa: E402  - used by _pid_alive; kept at the bottom to make the flake rule happy
