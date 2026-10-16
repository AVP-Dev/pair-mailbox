"""Neighbour liveness and readiness to read messages.

`_peer_status` answers the question both agents ask themselves every round:
is the neighbour still here, and will it read what I write now. The answer
serves the state reader, not the state changer — hence it lives here.

The module knows nothing of `PairSession`: state and own role in, a dict by
role out. Exactly why tests can call it without raising the server.
"""
from __future__ import annotations

import errno
import os
from datetime import datetime, timezone
from typing import Any


def pid_alive(pid: Any) -> bool:
    """Whether the process with this pid is alive.

    A separate function, not a server call: the neighbour-liveness test must
    not raise the whole MCP server to check one line.

    Two cases are unobvious and both moved verbatim from the old
    `server.is_pid_alive`, because rewrites lose them:

    - a sub-one or non-integer pid is not a process. `os.kill(-1, 0)` on Linux
      happily "checks" all processes at once and would return alive — a neighbour
      with a corrupt record declared living;
    - EPERM means "the process exists, but not mine". That is alive, not dead:
      the neighbour may run as another user, and declaring it dead means never
      waiting for the answer that arrives.
    """
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError as err:
        return err.errno == errno.EPERM
    return True

def proc_starttime(pid: Any) -> int | None:
    """The process's boot-relative start time (/proc/<pid>/stat field 22).

    Round 103 S3: `os.kill(pid, 0)` proves SOME process holds the pid, not
    that OUR process does — pids recycle, and the round-102 live case (a role
    server killed at 17:30Z kept a roster entry alive for three hours through
    plain pid reuse) is the outcome this closes. Read-only in /proc, no new
    files. Linux-only by nature; None on any failure (wrong platform, exited
    between os.kill and open) — callers fall back to pid_alive, never guess.
    """
    if not isinstance(pid, int) or pid <= 0:
        return None
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            stat = handle.read()
    except OSError:
        return None
    try:
        # After "(comm)" the fields restart at 3; starttime is field 22 →
        # index 19 in the post-comm split (field 3 == index 0 here).
        tail = stat.rpartition(b")")[2].split()
        return int(tail[19])
    except (IndexError, ValueError):
        return None


def pid_slot_alive(pid: Any, expected_start: Any) -> bool:
    """Whether OUR process (pid + recorded starttime) still holds the slot.

    The staleness rule of the round: a stored snapshot (pid alone) is read as
    current only when something recomputes it. A reused pid must read DEAD
    even though os.kill says alive. No recorded starttime (old roster entries)
    or an unreadable /proc keeps the coarser os.kill answer — stated fallback,
    never a guess in either direction on our own slot.
    """
    if not pid_alive(pid):
        return False
    start = proc_starttime(pid)
    if start is None or not isinstance(expected_start, int):
        # Fallback (stated): no /proc on this platform, the process exited
        # between probes, or a pre-S3 entry has no recorded start.
        return True
    return start == int(expected_start)


def peer_status(
    state: dict[str, Any],
    *,
    role: str,
    writer: str | None,
    alive_check=pid_alive,
    unread: dict[str, int] | None = None,
) -> dict[str, Any]:
    """What every other party does and whether it hears the new.

    The writer used to close the round with no idea whether the neighbour left,
    and then the finding landed in a closed round. Saying it aloud costs less
    than stacking a letter into nowhere.

    `alive_check` is stubbed in tests: the "neighbour dead" check must never
    depend on whether a PID lives at suite-run time.

    `unread` is unread counts by role (role → count), counted by the caller:
    the module never sees the mailbox. No count means no fields, not zeroes:
    zero would read "all read" while missing data means unknown.
    """
    SILENT_AGENT_GRACE = 30 * 60
    phase = state.get("phase", "scoping")
    now = datetime.now(timezone.utc)
    out: dict[str, Any] = {}
    for name, info in (state.get("roles") or {}).items():
        if name == role:
            continue
        age = None
        try:
            if info.get("last_seen"):
                age = int((now - datetime.fromisoformat(info["last_seen"])).total_seconds())
        except Exception:
            age = None
        closed = phase == "done"
        pid = info.get("pid")
        # Round 103 S3: alive NOW means "the recorded process still holds the
        # slot" — pid + its recorded /proc starttime. A reused pid reads DEAD
        # (proof of the round): os.kill alone proved "some process", and the
        # dead-for-3-hours roster entry was the measured cost.
        alive = None
        if pid:
            start = info.get("pid_start")
            alive = (pid_slot_alive(pid, start) if isinstance(start, int)
                     else alive_check(pid))
        # The role server lives but no agent stands behind it: the roster pid is the
        # server pid, and the server itself writes a fresh last_seen at start and while
        # serving foreign calls. A role that never sent anything for grace after
        # registration is mute: nobody reads a letter to it (R85, pi).
        mute = False
        try:
            born = info.get("first_seen")
            years_old = (int((now - datetime.fromisoformat(born)).total_seconds())
                         if born else 0)
        except Exception:
            years_old = 0
        if (alive is not False and not closed and years_old > SILENT_AGENT_GRACE
                and int(info.get("sends", 0) or 0) == 0):
            mute = True
        hint = "Listening."
        if closed:
            hint = (
                "The round is closed. This role has moved on and will not read new"
                " messages. To get it back: pair_advance('implement')."
            )
        elif alive is False:
            hint = f"Process is no longer running (PID {pid} is dead)."
        elif mute:
            hint = ("Server is up but the agent never spoke "
                    "(no sends since registration). Raise it before writing.")
        out[name] = {
            "role": "writer" if name == writer else "critic",
            "last_seen": info.get("last_seen"),
            "silent_seconds": age,
            "alive": alive,
            "round_closed": closed,
            "will_read_new_messages": not closed and (alive is not False)
            and not mute,
            "hint": hint,
        }
        if unread is not None and name in unread:
            out[name]["unread"] = unread[name]
    return out