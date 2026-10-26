"""The monologue detector: when an agent thinks out loud without touching a tool.

Why this module exists. Round 146 measured a four-hour hole: an agent spent
four hours in a monologue of reasoning — screen text changing, no tool ever
called — and the supervisor had no word for that state. `ComposerBusy` was
tried first and is the wrong signal: it is a fact about the INPUT FIELD (text
sitting in the composer), not about generation. A role that is thinking out
loud has an EMPTY composer; a role with a half-typed question has a busy one.
Reading `ComposerBusy` as "the agent is working, hands off" is exactly what
left the four hours unwatched.

So the signal is two facts measured together:

1. **The screen changes.** The pane's text differs from the previous tick, so
   the agent is producing output. A frozen screen is a different defect and
   `bin/pairpilot`'s `check_stalled_busy` already owns it (Escape + /compact).
2. **No tool subprocess is alive.** The role's own process tree holds no
   child beyond the agent CLI itself and this project's MCP server. A running
   test, a `git` call, a `gh` call is WORK, not a monologue.

Only a role that is streaming AND tool-free for longer than the threshold is
cut. Everything here is a pure function over facts the caller supplies, so the
rule can be tested without tmux, without a pane and without an agent.

What this module does NOT decide: whether to interrupt. That belongs to the
caller, which alone knows whether the pane is provably ours and whether the
role's own CLI is in it (PROTOCOL.md §7c).
"""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

# The braille block: every TUI here draws its spinner from it, and the spinner
# ROTATES while the agent waits for the network. Hashing the raw screen would
# call that wait "the agent is producing output" — the exact false positive
# that would make the detector cut an agent which is merely waiting for an
# answer. Normalised to one glyph, so only real content moves the signature.
SPINNER_PATTERN = re.compile(r"[⠀-⣿]")

# The elapsed timer of a running tool. It changes every tick while the tool
# runs, and it would read as streaming. The tool's own process is the real
# signal there, and the timer is not content.
ELAPSED_PATTERN = re.compile(r"elapsed\s+\d+(?:\.\d+)?s", re.IGNORECASE)

# The wall clock in a status line. Same reason as the elapsed timer: it moves
# on its own, and it says nothing about the agent.
CLOCK_PATTERN = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b")

# The default threshold, in seconds: five minutes of streaming with no tool
# call is a monologue, not a long thought. Measured against real work — the
# slowest legitimate tool call observed in this project ran a full test suite
# for a couple of minutes, and that suite keeps a subprocess alive the whole
# time, so the tool exemption already covers it.
DEFAULT_MAX_BUSY_SECONDS = 300.0

# This project's MCP server is a long-lived child of the agent CLI: it answers
# pair_read, pair_send and the rest for the whole session. Counting it as a
# tool would exempt every role forever and the watchdog would never fire.
#
# It is recognised by argv SHAPE (`server.py` plus `--role <role>`), not by a
# substring of the role name: a substring match would count somebody else's
# server as ours and hide a real tool.
ROLE_SERVER_MARKER = "server.py"

# The agent CLI itself is never a tool: it is the thing doing the monologue,
# not a tool it runs. These are the CLIs this project supports
# (cli-profiles.json). The name is matched on the executable's basename, so
# `/home/x/.bun/bin/omp` counts as `omp`.
AGENT_CLI_NAMES = frozenset({
    "omp", "agy", "opencode", "codex", "claude", "pi",
})


def read_cmdline(pid: int) -> list[str]:
    """The argv of a pid, or [] when it is gone.

    A dead pid is not an error: the caller asks about a process tree that may
    change under it, and "no argv" simply means "not there".
    """
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except (OSError, TypeError, ValueError):
        return []
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]


def is_role_server(pid: int, role: str) -> bool:
    """Whether the process is this project's MCP server for `role`.

    Both the file name and the neighbour args are checked. A bare role
    substring is banned: an error here would count somebody else's server as
    ours, and the watchdog would then never fire for that role.
    """
    args = read_cmdline(pid)
    if not any(part.endswith(ROLE_SERVER_MARKER) for part in args):
        return False
    for index, part in enumerate(args[:-1]):
        if part == "--role" and args[index + 1] == role:
            return True
    return False


def is_agent_cli(pid: int) -> bool:
    """Whether the process is the agent CLI itself rather than a tool it runs.

    The CLI may be launched through a runtime: the live omp process is
    `bun /home/alexios/.bun/bin/omp`, so the executable's basename is `bun`
    and the real name is one argument later. Without the second name the
    agent's own process would count as a tool, and the detector would never
    fire for the very CLI it watches.
    """
    args = read_cmdline(pid)
    if not args:
        return False
    if Path(args[0]).name in AGENT_CLI_NAMES:
        return True
    if Path(args[0]).name == "bun" and len(args) > 1:
        return Path(args[1]).name in AGENT_CLI_NAMES
    return False


def tool_pids(pids: list[int], role: str) -> list[int]:
    """The pids among `pids` that are a TOOL: work the agent is actually doing.

    Everything that is neither the agent CLI's own process nor this project's
    MCP server counts as a tool. A throwaway `python3 -` script (how an agent
    probes something) is a tool: it is work in progress.
    """
    out: list[int] = []
    for pid in pids:
        if is_agent_cli(pid) or is_role_server(pid, role):
            continue
        if not read_cmdline(pid):
            # The pid is gone between the scan and the read: not a tool.
            continue
        out.append(pid)
    return out


def descendants(root: int) -> list[int]:
    """Every descendant of `root`, breadth-first.

    The pane's process tree is the evidence: the agent CLI, its MCP server and
    whatever tool is running all live under the pane's shell. The tree is read
    from `/proc`, so it is the truth about this machine and not a guess about
    what a tool is called.
    """
    if not root:
        return []
    children: dict[int, list[int]] = {}
    try:
        entries = os.listdir("/proc")
    except OSError:
        return []
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            stat = Path(f"/proc/{entry}/stat").read_text()
            ppid = int(stat.rpartition(")")[2].split()[1])
        except (OSError, IndexError, ValueError):
            continue
        children.setdefault(ppid, []).append(int(entry))
    out: list[int] = []
    frontier = [root]
    while frontier:
        current = frontier.pop(0)
        for child in children.get(current, []):
            out.append(child)
            frontier.append(child)
    return out


def screen_signature(lines: list[str]) -> str:
    """A fingerprint of the pane's CONTENT, without its volatile parts.

    Three things change on a screen without the agent having said anything:

    - the spinner, which rotates while the CLI waits for the network — a wait
      is NOT output, and treating it as output would make the detector cut an
      agent that is merely thinking about an answer;
    - the elapsed timer of a running tool — the tool's own process is the
      signal there, and the timer is not content;
    - a wall clock in the status line.

    Everything else is content: a growing transcript, a new tool call, a new
    line of reasoning. Only content may open a monologue streak.
    """
    text = "\n".join(lines)
    text = SPINNER_PATTERN.sub("*", text)
    text = ELAPSED_PATTERN.sub("elapsed <n>s", text)
    text = CLOCK_PATTERN.sub("<time>", text)
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


class MonologueTracker:
    """How long each role has been streaming without touching a tool.

    The tracker holds one fact per role: when the current streaming streak
    began. A streak ends the moment the screen stops changing or a tool
    appears — both are the agent's business, and neither is the supervisor's.

    The streak is measured in wall-clock between the observations the caller
    supplies, and the caller passes the tick's own timestamp. A supervisor
    that slept for an hour between ticks therefore sees an hour-long gap —
    which is the honest reading of "this has been going on for an hour", and
    not a claim the tracker invented.
    """

    def __init__(self, threshold: float = DEFAULT_MAX_BUSY_SECONDS) -> None:
        self._streaks: dict[str, float] = {}
        self.threshold = max(1.0, float(threshold))

    def observe(self, role: str, streaming: bool, has_tool: bool,
                now: float) -> dict:
        """Record one observation and return the verdict for that role.

        `streaming` means "the pane's text changed since the previous tick".
        `has_tool` means "a tool subprocess is alive in the role's tree".

        The verdict is one of:
          - `idle`      — no streaming streak (nothing is happening);
          - `working`   — streaming, but a tool is alive (real work);
          - `thinking`  — streaming with no tool, below the threshold;
          - `monologue` — streaming with no tool, at or past the threshold.
        """
        if not streaming or has_tool:
            self._streaks.pop(role, None)
            if streaming and has_tool:
                return {"verdict": "working", "seconds": 0.0}
            return {"verdict": "idle", "seconds": 0.0}
        since = self._streaks.get(role)
        if since is None:
            self._streaks[role] = now
            return {"verdict": "thinking", "seconds": 0.0}
        seconds = max(0.0, now - since)
        verdict = "monologue" if seconds >= self.threshold else "thinking"
        return {"verdict": verdict, "seconds": seconds}

    def streak_seconds(self, role: str, now: float) -> float:
        """How long the current streaming streak of `role` has lasted."""
        since = self._streaks.get(role)
        if since is None:
            return 0.0
        return max(0.0, now - since)

    def forget(self, role: str) -> None:
        """Drop the streak of a role — it is gone, or its pane is not ours."""
        self._streaks.pop(role, None)

    def reset(self) -> None:
        """Forget every streak. Used when a supervisor restarts."""
        self._streaks.clear()
