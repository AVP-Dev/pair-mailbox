#!/usr/bin/env python3
"""Types a message into the neighbour agent composer and verifies it landed.

This ports the umputun/agterm approach onto tmux. Needed when the agent must not
just wake but receive the message as an ordinary line in its window.

Why not "just send keys":

- the composer may be busy. Typing over the neighbour keystrokes eats its text;
- long text breaks when pasted whole — chunked, as theirs;
- the paste may fail. Then a rollback is due, or garbage stays in the prompt.

So: snapshot → "free" check → chunks → tail verify → Enter or rollback.
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import time
import json
from pathlib import Path

# A live line scrolls; take the snapshot window with margin.
CAPTURE_LINES = 40

# Bytes per paste. umputun runs 197: at 1627 bytes it still lost windows of
# 1022 at the 1663rd. Slightly less is safer.
CHUNK_BYTES = 170

# How many times to re-read the pane before calling the paste unconfirmed.
# Measured, not chosen: in a plain shell the text shows in capture in 0.05s,
# while in a heavy TUI (Gemini in a 30x70 pane) a lone BSpace lands in ~2s.
# The 8 x 0.4 = 3.2s window covers the worst case with margin. One read 0.35s
# after typing is a stale frame more often than a missing paste.
VERIFY_ATTEMPTS = 8
VERIFY_PAUSE = 0.4

# How many lines above the question feed the verdict. On Claude Code the command
# sits second to last, so at least one is needed; two with margin, because
# different CLIs sometimes frame between the command and the question.
OVERSCAN_LINES = 3

# Corner glyphs of the input-field BOTTOM edge. Pane side frames use the
# `┃`/`│` vertical and deliberately miss this set: a pane never tells by the
# vertical, but by the bottom corner it does.
INPUT_BOX_CORNERS_LOWER = "\u2579\u258c\u2588"

INPUT_BOX_CORNERS = "\u2579\u257a\u2578\u2576\u257b\u2502\u2503\u2551\u2552"

# What counts as a text-entry invitation. Universal: the last line where the
# marker stands followed by empty or near-empty. `❯` is the OpenCode composer
# marker: without it the TUI-hint cleanup branch in is_composer_empty stayed
# unreachable, and a pane trailing "⇧⇥ to change thinking effort" read busy
# forever — the pilot never woke the sleeping agent (.pair/notes/finding.md, idle_roles).
PROMPT_MARKERS = (">", "\u203a", "$", "#", "::", "ask>", "╰─", "\u276f")

# TUI hint arrows in the input line. Scrubbed as glyphs: they carry no agent-text
# meaning, while OpenCode/Codex hint tails always stand there.
TUI_HINT_ARROWS = re.compile("[\u21e7\u21e5\u2191\u2193\u2192\u2190]")

# Known hint phrases whole. Matching checks the WHOLE remainder of the line past
# the marker: the prefix form (`ctrl\+x.*`) declares a composer with real text
# "ctrl+x does not help: text" empty, and the pilot would overwrite another input.
# A new hint kind joins here explicitly — no silent widening.
# widening allowed.
TUI_HINT_PHRASES = (
    r"to change thinking effort",
    r"to change model",
    r"to scroll",
    r"commands?",
    r"ctrl\+[a-z]\s+[a-z][a-z ]*",
    r"(?:[a-z]+\s+)*to (?:change|scroll|select|confirm|switch|exit|navigate|toggle)",
)


def looks_like_status_line(line: str) -> bool:
    """The TUI status line reads structurally, with no brand names.

    A brand-mark fix never holds: the first theme change returns the original bug.
    The only stable mark is `$` immediately followed by a value — a price: `$0.00`.
    A real `$`-prompt ends in a mark, as in `user@host:~$`, and no mark may stand
    at the line end — or delivery into a plain shell stops working.
    """
    text = line.strip()
    if not text:
        return True
    if text[0] in INPUT_BOX_CORNERS_LOWER or text.startswith("╰─"):
        return False
    if re.fullmatch(r"[\u2500-\u259f\s═─━╹▀_-]+", text):
        return True
    if text.startswith("$"):
        return False
    if "$" not in text:
        return False
    return any(char.isdigit() for char in text.split("$", 1)[1][:3])


class ComposerBusy(Exception):
    pass


def _send_keys(tmux: str, pane: str, *keys: str) -> subprocess.CompletedProcess:
    """One `tmux send-keys`, with a timeout that reads as BUSY, not as death.

    The measured crash (2026-10-25, this project's own supervisor): `tmux
    send-keys` into a pane whose CLI had stopped consuming input hung for
    the full 5-second timeout and raised `subprocess.TimeoutExpired`. The
    pilot's `flush_outbox` caught `ComposerBusy` and nothing else, so the
    exception escaped, the whole supervisor died, and EVERY rule it carried
    — including the monologue watchdog — went dark with it. A dead
    supervisor is the one failure the watchdog cannot notice.

    So a tmux timeout is what it physically is: the pane did not answer in
    time. It becomes `ComposerBusy`, the one exception every caller already
    knows how to handle, and the delivery is simply skipped for this tick.
    """
    try:
        return subprocess.run([tmux, "send-keys", "-t", pane] + list(keys),
                              capture_output=True, text=True, timeout=5)
    except subprocess.TimeoutExpired as expired:
        raise ComposerBusy(
            f"tmux send-keys timed out into {pane}: {expired}") from expired


def _tmux() -> str:
    """tmux, prefixed with the project's socket when one is named.

    Condition (c) of the per-project-socket slice (round 106): ONE resolver for
    every tmux call site. A single tmux server serves every project on the box,
    so plain `tmux` makes another project's panes visible and addressable here —
    that is how a message once landed in galatour's pane. The socket name comes
    from PAIR_PANE_SOCKET (exported by pairup from the resolved project root) and
    is never read from state.json, so binding needs no state file.

    Legacy fallback: with no socket named, plain tmux — the live pair on the
    default socket stays reachable, and the transition never blinds it.
    """
    exe = shutil.which("tmux")
    if not exe:
        raise ComposerBusy("tmux not found in PATH")
    socket = (os.environ.get("PAIR_PANE_SOCKET") or "").strip()
    if socket and socket != "default":
        return f"{exe} -L {socket}"
    return exe


def pane_alive(tmux: str, pane: str) -> bool:
    """Whether the tmux pane lives, and whether it is a pane at all.

    A line unlike a pane is no pane: without this check tmux answered `-t opencode`
    (a process name, not a reference) with code 0 and EMPTY output, while the
    "alive" verdict derived from the `alive` field — true for such an answer. The
    shape check stands BEFORE talking to tmux exactly for that: never answer for
    what was never asked.

    The rule matches server.pane_exists exactly, and that is no coincidence: tmux
    answers a missing-pane query with code 0 and EMPTY output, so the "success code
    and not \"1\"" check declares any phantom pane alive. The fix used to live in
    server.pane_exists alone, while pairpilot calls this function — and phantom
    %999999 counted as living, so pilot_pane_dead never wrote. The drift of two
    same-named functions was the hole.

    So both functions call pane_identity instead of reading tmux themselves: one
    thought — one place, or fixing one drops the agreement check.
    """
    if not pane_ref_format(pane):
        return False
    try:
        ident = pane_identity(tmux, pane)
    except (OSError, subprocess.SubprocessError):
        return False
    return bool(ident["exists"] and ident["alive"])


# Two pane-reference formats, both always accepted: `%N` and `<session>:<win>.<pane>`.
# The second is stabler: the number runs through one tmux server, renumbers on
# pane recreation and goes stale in the process environment, while the session
# name with window and pane never lives that short.
#
# Backward compatibility is no politeness: live roles carry `%N` in state, and had
# the record stopped taking them, the upgrade would break delivery for all at once.
# So the preferred format writes, and both read.
PANE_ID_FORMAT = "id"
PANE_ADDRESS_FORMAT = "address"

PANE_ID_RE = re.compile(r"%\d+\Z")
# A tmux session name never holds `:` (checked: `new-session -s 'a:b'` refuses),
# while `.` inside turns into `_`. So the colon splits the session name unambiguously,
# and `\d+\.\d+` at the tail means window and pane.
PANE_ADDRESS_RE = re.compile(r"(?P<session>[^:]+):(?P<window>\d+)\.(?P<pane>\d+)\Z")


def pane_ref_format(pane: str) -> str:
    """Which reference shape this is: `id`, `address`, or "" (no pane at all).

    A separate function, because its result decides the call fate instead of
    reporting a fact: `_ring` refuses to ring our own pane on string comparison,
    while `pairpilot` refuses to touch a pane held by another role. Both checks
    must parse both shapes, or past the session-address move they silently stop firing.
    """
    text = str(pane or "")
    if PANE_ID_RE.fullmatch(text):
        return PANE_ID_FORMAT
    if PANE_ADDRESS_RE.fullmatch(text):
        return PANE_ADDRESS_FORMAT
    return ""


def session_address(tmux: str, pane: str) -> str:
    """The stable pane address: `<session>:<window>.<pane>`, or "" when it fails.

    A measured, important limit: the session name isolates projects ONLY when every
    project owns its session. On a shared session `mail:0.0` may belong to another
    project, caught only by the `pane_current_path` check. So the session address
    and the path check are two independent lines, never one replacing the other.
    """
    r = subprocess.run(
        [tmux, "display-message", "-p", "-t", pane,
         "#{session_name}:#{window_index}.#{pane_index}"],
        capture_output=True, text=True, timeout=5)
    out = r.stdout.strip()
    if r.returncode != 0 or not out or out.startswith(":"):
        return ""
    # MEASURED, and it breaks the naive take: tmux answers a missing pane with code
    # 0, not an error, and RENAMES the query onto the neighbouring live pane of the
    # same window. A two-pane session, pane `.1` dead, and `display-message -t S:0.1`
    # calmly returns `S:0.0`. On `%N` nothing like that: `display-message -t %499999`
    # gives empty output.
    #
    # So an address is never taken or recorded unchecked against what was asked:
    # otherwise a stale address quietly points at another pane, and the undelivered
    # message leaves to the neighbour instead of a refusal.
    if pane_ref_format(pane) == PANE_ADDRESS_FORMAT and out != str(pane):
        return ""
    return out


def same_pane(tmux: str, left, right) -> bool:
    """One pane recorded in two shapes. Fail-closed.

    String comparison was the quiet breakage here: our own pane arrives from the
    environment as `%1` while state holds `mail:0.1`, and `pane == TMUX_PANE` stopped
    holding. Checks built on that equality never fell — they just stopped firing:
    `_ring` started ringing our own pane, while `pairpilot` read another pane as held
    and left it alone while the real neighbour sat inside.

    Both references resolve to the session address: the only name tmux gives both
    shapes. When the address never resolves, exact string comparison stays — two
    identical session addresses are still one pane, and refusing here would break
    delivery to the neighbour sitting next door.
    """
    if not left or not right:
        return False
    if str(left) == str(right):
        return True
    if not (pane_ref_format(left) and pane_ref_format(right)):
        return False
    left_addr = pane_identity(tmux, left).get("address")
    right_addr = pane_identity(tmux, right).get("address")
    # An empty address means "no pane", and two such references never count as
    # different panes: otherwise a phantom would match a phantom.
    return bool(left_addr) and left_addr == right_addr


def pane_key(tmux, pane) -> str:
    """The pane-grouping key, identical for both reference shapes.

    Needed where panes compare pairwise or group into a table: past the move to
    session addresses `%1` and `mail:0.1` are one pane, while string comparison saw
    two. Past that it broke the supervisor `ambiguous()`, which — missing two roles
    on one pane — called it free and struck another composer.

    tmux unreachable or no pane means the source line: a grouping key, not a
    verdict, and worse than before never follows.
    """
    if not pane or not tmux:
        return str(pane or "")
    try:
        address = pane_identity(tmux, pane).get("address")
    except (OSError, subprocess.SubprocessError):
        return str(pane)
    return address or str(pane)


def pane_identity(tmux: str, pane: str) -> dict:
    """Who exactly sits in the pane: not "alive or not" but "ours or not".

    One `#{pane_dead}` never suffices, and that was the cross-project hole. The `%N`
    number outlives the death of the process that held it: the pane lives but belongs
    to another project already. Several pair-mailbox projects run on this machine, so
    ringing the state-recorded number landed in another composer, with one project
    typing its messages into another agent.

    One tmux query answers everything at once, and that is no saving for saving’s
    sake: `#{pane_dead}` and `#{session_name}` come from one pane snapshot. Two
    separate calls would mean two different times, with the pane possibly dead
    between them, and an "alive, ours" verdict assembled from two snapshots of
    different panes.
    """
    r = subprocess.run(
        [tmux, "display-message", "-p", "-t", pane,
         "#{pane_dead}\t#{pane_current_path}\t#{session_name}:#{window_index}.#{pane_index}\t#{session_id}\t#{pane_id}"],
        capture_output=True, text=True, timeout=5)
    # tmux answers a NONEXISTENT pane with code 0 and empty output — exactly the
    # same fact over which the phantom pane once counted as living. So return code
    # alone never suffices: empty output also means no pane. `exists` used to derive
    # from returncode alone and lied: the total stayed safe by accident, not design.
    # An empty FLAT cwd field stays a nonempty answer: `pane_current_path` may run
    #
    # empty on a live pane, and that is "unproven", not "missing" — different refusals
    # with different causes, never to confuse.
    # The `\d+\.\d+` tail means window and pane.
    parts = r.stdout.strip("\n").split("\t")
    dead = parts[0].strip() if parts else ""
    if r.returncode != 0 or not dead:
        return {"exists": False, "alive": False, "path": "", "address": ""}
    path = parts[1].strip() if len(parts) > 1 else ""
    address = parts[2].strip() if len(parts) > 2 else ""
    # The same deception as in session_address, only now it strikes the "is it ours"
    # line: a stale session address returns the NEIGHBOUR pane cwd and alive.
    # and the path check passed, printing would go to another composer. Reconcile the
    # address with what was asked: on mismatch there is simply no pane.
    if address and pane_ref_format(pane) == PANE_ADDRESS_FORMAT and address != str(pane):
        return {"exists": False, "alive": False, "path": "", "address": ""}
    if not address:
        address = session_address(tmux, pane)
    extra = parts[3:]
    return {"exists": True, "alive": dead in ("0", "false"), "path": path,
            "address": address, "session_id": extra[0].strip() if len(extra) > 0 else "",
            "pane_uid": extra[1].strip() if len(extra) > 1 else ""}


def _ancestor_chain(pid: int) -> set:
    """The pid-ancestor chain over /proc — the shared root of both pane searches.

    An empty set instead of [""]: the "unknown" vs "known absent" distinction is
    needless here, both read "pane not found". pid<=1 drops on its own: init has
    no pane ancestors, and the loop never starts.
    """
    chain: set = set()
    try:
        me = int(pid)
    except (TypeError, ValueError):
        return chain
    while me > 1 and me not in chain:
        chain.add(me)
        try:
            stat = Path(f"/proc/{me}/stat").read_text()
            me = int(stat.rsplit(")", 1)[1].split()[1])
        except (OSError, IndexError, ValueError):
            break
    return chain


def find_pane_of_pid(tmux: str, pid: int) -> str:
    """The pane in whose tree the pid lives — or "".

    The reverse path: the pid ancestor chain up to some live pane pane_pid.
    Needed twice: the role finds itself with no TMUX_PANE (find_own_pane), the
    pilot finds another role by roster pid. Off Linux or with no pane — an honest "".
    """
    chain = _ancestor_chain(pid)
    if not chain:
        return ""
    try:
        out = subprocess.run([tmux, "list-panes", "-a", "-F", "#{pane_id} #{pane_pid}"],
                             capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return ""
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].isdigit() and int(parts[1]) in chain:
            return parts[0]
    return ""


def find_own_pane(tmux: str) -> str:
    """The pane whose process tree calls — or "".

    TMUX_PANE may miss in the MCP server environment (inherited from the CLI),
    while a paneless roster blinds deliver/unblock: the role lives but nowhere to
    wake. A thin find_pane_of_pid call on our own pid.
    """
    return find_pane_of_pid(tmux, os.getpid())


def pane_is_foreign(tmux: str, pane: str, project_root) -> tuple:
    """Is the pane provably not ours? Fail-closed: unproven ours means no printing.

    The logic moved to `panes`: same birth race and the "foreign project" vs "dead
    pane" split live there. The call stays here because `pair_deliver` and `pairctl`
    call this name, and renaming it in four places for one move is needless.

    The import stays lazy for the same reason as the rest here: `panes` takes
    `pane_identity` from `composer`, and a top-level import would cycle.
    """
    from panes import pane_is_foreign as impl

    return impl(tmux, pane, project_root)


def pane_belongs_to_project(tmux: str, pane: str, project_root) -> tuple:
    """Whether the pane belongs to this project. The logic lives in `panes`."""
    from panes import pane_belongs_to_project as impl

    return impl(tmux, pane, project_root)


def pane_pid(tmux: str, pane: str) -> int:
    """The PANE PROCESS pid — usually a shell, not the agent. Not "the role pid".

    Returning it under "the pid of the process holding the pane" would hold only
    formally. Measured on live panes: `#{pane_pid}` in an agent pane returns bash,
    because the agent lives as a shell child. Two troubles follow calling the field
    what it never was:

    - the roster, holding the role-server pid, took the shell pid, and values
      started flipping every tick as `_register` overwrote its own;
    - worse: a fallen agent leaves a LIVE shell behind, so "someone sits in the
      pane" always holds, and a fallen role would never restart.

    For "is the role alive" there is `pane_role_pid`. This function stays as the
    bottom layer: its honest name is the pane process, and nothing more.
    """
    ident = pane_identity(tmux, pane)
    if not (ident["exists"] and ident["alive"]):
        return 0
    r = subprocess.run(
        [tmux, "display-message", "-p", "-t", pane, "#{pane_pid}"],
        capture_output=True, text=True, timeout=5)
    out = r.stdout.strip()
    if r.returncode != 0 or not out.isdigit():
        return 0
    pid = int(out)
    return pid if pid > 0 else 0


def _cmdline(pid: int) -> list:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]


def _is_role_server(pid: int, role: str) -> bool:
    """Whether the process is `server.py --role <role>`, or not.

    Both the file name and the neighbour args check. Searching a bare role
    substring is banned: an error here would cost another pid in another record —
    exactly the breakage class the path check already caught across projects.
    """
    args = _cmdline(pid)
    if not any(part.endswith("server.py") for part in args):
        return False
    for index, part in enumerate(args[:-1]):
        if part == "--role" and args[index + 1] == role:
            return True
    return False


def _descendants(root: int) -> list:
    """All child processes of `/proc`, breadth-first.

    Order matters: the nearest ancestor goes first, and with several candidates
    the one closer to the pane wins, not the bigger pid.
    """
    children: dict = {}
    try:
        entries = os.listdir("/proc")
    except OSError:
        return []
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            stat = Path(f"/proc/{entry}/stat").read_text()
            ppid = int(stat.rsplit(")", 1)[1].split()[1])
        except (OSError, IndexError, ValueError):
            continue
        children.setdefault(ppid, []).append(int(entry))
    out, frontier = [], [root]
    while frontier:
        current = frontier.pop(0)
        for child in children.get(current, []):
            out.append(child)
            frontier.append(child)
    return out


def find_role_pid(root: int, role: str) -> int:
    """The role server among `root` descendants, or 0.

    Split from `pane_role_pid` on purpose: tmux here only learns the pane pid,
    while the whole "who sits in the pane" question resolves over `/proc`. The
    split serves checkability — merging them was exactly what let the bug live:
    the function checked by a stub that never touched the code below.
    """
    if not root or not role:
        return 0
    for candidate in _descendants(root):
        if _is_role_server(candidate, role):
            return candidate
    return 0


def pane_role_pid(tmux: str, pane: str, role: str) -> int:
    """The ROLE server pid, when its process lives in this pane, else 0.

    The actual question the supervisor needs, and it never equals `pane_pid`:
    a shell always sits in the pane, even after the agent fell, so "someone in
    the pane" never proves "the role lives".

    Search the role server, because its pid is what `_register` writes
    (`os.getpid()` of the server) and what `is_pid_alive` awaits in the roster.
    Hence the meaning: the record update returns a same-kind value, or pids of
    two different processes would fight over one field.

    One tmux query for everything: pane facts come from `pane_identity`, not a
    second `display-message`. The pane may die between two calls, and the verdict
    would assemble from snapshots of different panes.

    Zero means "not found", never an error: the caller must read it as "unknown",
    not "no pane". Off Linux no process tree exists, and the honest answer is 0.
    """
    return find_role_pid(pane_pid(tmux, pane), role)


def capture(tmux: str, pane: str) -> list:
    """The pane's visible lines, with soft-wrapped lines joined.

    `-J` (tmux >= 2.3) restores the LOGICAL line: a line the terminal wrapped for
    width comes back whole. Without it every regex over the pane read a fragment —
    a permission prompt split across rows never matched.
    Measured on tmux 3.6: `-J` joins only lines the terminal itself wrapped, so
    the frame geometry survives; an old tmux without `-J` falls back to the plain
    capture, because an empty pane reads as "busy".
    """
    r = subprocess.run([tmux, "capture-pane", "-p", "-J", "-t", pane],
                       capture_output=True, text=True, timeout=5)
    if r.returncode != 0:
        r = subprocess.run([tmux, "capture-pane", "-p", "-t", pane],
                           capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            return []
    return r.stdout.splitlines()[-CAPTURE_LINES:]


# TUI frames: box-drawing and block glyphs. One constant on purpose: the
# "is it frame part?" question rang in two places in two different ranges —
# 2500-259f in one and 2500-257f in another — and the one-char difference was
# exactly what read a busy composer as empty. The same lesson as the
# pane_alive / server.pane_exists drift: one thought, one place.
BOX_GLYPH = r"\u2500-\u259f"


def is_input_box_hint(line: str) -> bool:
    """The empty-input-field hint (Ask anything…): no text typed."""
    return "ask anything" in line.strip().lower()


def is_input_box_border(line: str) -> bool:
    """The TUI input-field bottom edge: a frame widening as you type.

    A structural mark, with no brand names: the line holds only frame and block
    glyphs. Needed because OpenCode owns no ASCII prompt marker: the input field
    is a frame, with a status line below. The composer was sought only as "a line
    starting with a marker", so an OpenCode pane never recognized and silently
    counted as busy — delivery and ringing into it were impossible in principle,
    not occasionally.
    """
    text = line.rstrip()
    if not text.strip():
        return False
    if re.search(r"[^\s" + BOX_GLYPH + r"|]", text):
        return False
    return True


def is_footer_or_status_line(line: str) -> bool:
    """Whether the line is a service footer or a TUI frame."""
    s = line.strip()
    if not s:
        return True
    # Clean horizontal separators and frames.
    if re.fullmatch(rf"[{BOX_GLYPH}\s═─━╹▀_-]+", s):
        return True
    return False


def composer_line(lines: list) -> str | None:
    """The last line a human may type into, or None when no composer shows.

    Three pane shapes, and an error in any one silently disables all delivery.

    **Bare shell.** The last nonempty line is the prompt.

    **TUI with the status line UNDER the composer.** The composer is the last
    line starting with a prompt marker; everything below is scenery.

    **TUI with a frame input field** (opencode). No prompt marker at all: the
    input field is a frame, with a status line below. The composer used to be
    sought only as "a marker line", so such a pane never recognized, the function
    returned None, and None by the caller contract means "busy" — delivery and
    ringing into opencode were impossible in principle, not occasionally.

    Priority runs exactly so: the input-field frame beats the marker line. The TUI
    draws a collapsed tool call as `› pair.pair_send (...)`, and `›` sits in
    PROMPT_MARKERS, so such a line reads as a prompt. But no composer it is, and
    over it the opencode pane counted as busy forever, even right after text landed
    in it.

    None means "no composer visible", never "free": a one-frame pane takes typing
    nowhere, and reporting it free sends keystrokes into nowhere. The caller must
    count None as busy.
    """
    # opencode geometry bottom-up: the path/status line, above it the input-field
    # bottom edge (the `╹`/`▌` corner). A pi-like frame: content BETWEEN two full
    # `───` separators, with status (cwd, tokens) below.
    # The first content line PAST the skipped separator is the input field (pi, live
    # capture 2026-10-10: typed text between two ─-rows). Without this mark pi read
    # busy forever.
    seen_sep_below = False
    low_idx = None
    for i, line in enumerate(lines):
        stripped = line.rstrip()
        if not stripped.strip():
            continue
        text = stripped.lstrip()
        # Art is no edge: a splash logo (`██ v18.11.1`) leads with the full
        # block but carries text — a real edge leads with ╹/▌, or is frame
        # glyphs only. The border demand touches █-led lines alone: typed
        # text inside a ╹-frame (`╹▀▀ typing ▀▀`) stays a busy composer
        # (frozen: test_a_typed_border_is_a_busy_composer).
        if text and text[0] in INPUT_BOX_CORNERS_LOWER:
            if text[0] == "█" and not is_input_box_border(stripped):
                continue
            low_idx = i
    if low_idx is not None:
        # Frame lines above the bottom edge; status/footer below.
        # A hint marks an empty field; without it the edge alone means a composer
        # (frozen: TestInputBoxCornerVsSideFrame, TestComposerOfAnOpencodePane). But typed
        # text above the edge means a busy field: then no edge, but input (test_composer:
        # frozen contract).
        # test_opencode_typed_text_reads_busy).
        hint = None
        for line in lines[:low_idx]:
            s = line.rstrip()
            if is_input_box_hint(s):
                hint = line
                continue
            tx = s.lstrip()
            if not tx.strip() or tx[0] not in INPUT_BOX_CORNERS:
                continue
            if is_input_box_border(s):
                continue
            stripped = re.sub(r"^[\u2500-\u257f\u2588|]+", "", tx).strip()
            # Build-line chrome — TUI chrome inside the field (frozen: "Build · something",
            # live idle: "Build auto · …").
            # Edge case: user text starting with "build" first reads free — taken for the
            # frozen contract.
            if stripped and not stripped.lower().startswith("build"):
                return None
        if hint is not None:
            return hint
        return lines[low_idx].rstrip()
    for line in reversed(lines):
        stripped = line.rstrip()
        if not stripped.strip():
            continue
        text = stripped.lstrip()
        if looks_like_status_line(stripped):
            is_sep = bool(re.fullmatch(r"[\u2500\u2550\u2501\s]+", text))
            if is_sep and seen_sep_below:
                # Two HORIZONTAL rows in a row (─ ═ ━, no side frames): an EMPTY input field
                # lies between (pi idle).
                # opencode side frames (' ┃') are no separators — the mark never fires on them
                # (test_protocol 1354, regression).
                return line
            if is_sep:
                seen_sep_below = True
            continue
        if text and text[0] in INPUT_BOX_CORNERS and is_input_box_border(stripped):
            continue
        if text.startswith("~/") or text.startswith("/"):
            continue
        if seen_sep_below:
            # A content line between separators — the pi style.
            return line
        break

    fallback = None
    above = None
    for line in reversed(lines):
        stripped = line.rstrip()
        if not stripped.strip():
            continue
        if fallback is None:
            fallback = stripped
        elif above is None:
            above = stripped
        for marker in PROMPT_MARKERS:
            if stripped.lstrip().startswith(marker):
                return stripped

    # No marker at all. Then either a clean TUI frame with nowhere to type (None —
    # busy), or an unknown pane kind — a bare shell — where the last line is the
    # prompt, as before all these fixes.
    return None if is_footer_or_status_line(fallback or "") else (
        None if looks_like_status_line(fallback or "") else fallback)

def composer_frame_present(lines: list) -> bool:
    """A composer frame shows, even when no input line is free.

    Label-only: delivery decisions never read this — `composer_line is None`
    stays hands-off either way. It only tells the journal whether None meant
    \"no composer at all\" (invisible) or \"a frame with nowhere to type\"
    (busy-with-chrome, e.g. an activity line read as typed text).
    """
    for line in lines:
        s = (line or "").rstrip()
        if not s.strip():
            continue
        text = s.lstrip()
        if text and text[0] in INPUT_BOX_CORNERS_LOWER:
            return True
        if re.fullmatch(r"[\u2500\u2550\u2501\s]+", text):
            return True
    return False


def ring_refusal_reason(shot: list, line: str | None) -> str | None:
    """Why the ring must not print into this pane, or None when it may.

    Lives beside composer_line (server-py-still-a-monolith): the refusal
    wording and the frame signal stay in one place. Decisions read this;
    the journal label comes along — None still means hands-off either way.
    """
    if line is None:
        return ("the neighbour composer busy-with-chrome, never interrupting"
                if composer_frame_present(shot) else
                "the neighbour composer invisible, never interrupting")
    if not is_composer_empty(line):
        return "the neighbour composer busy, never interrupting"
    return None

def is_composer_empty(line: str) -> bool:
    """A rough but honest check: almost nothing past the marker."""
    s = line.rstrip()
    if not s.strip():
        return True
    # An empty TUI frame: '  ┃  ', '  │  ' or the OpenCode input-field bottom edge.
    # The range here is deliberately BOX_GLYPH, not the old 2500-257f: ▀ is 2580, one
    # past that edge, so the input-field frame never passed and the empty OpenCode
    # composer read as busy. The check lives in is_input_box_border, with no duplicate
    # here on purpose: two identical checks in two places is exactly the drift class
    # that already cost us broken delivery.
    if is_input_box_border(s):
        return True
    if is_input_box_hint(s):
        return True  # an empty-field hint — not input
    for marker in PROMPT_MARKERS:
        idx = s.rfind(marker)
        if idx != -1:
            tail = s[idx + len(marker):].strip()
            # TUI frame, separator frames, cursor
            tail = re.sub(r"[\u2500-\u257f\u2588|]", "", tail).strip()
            # TUI hints and shortcuts at the input-line end (OpenCode / Claude Code
            # / Codex): "⇧⇥ to change thinking effort", "ctrl+x commands". Arrows leave
            # as glyphs, while words only match when the WHOLE remainder equals a known
            # phrase. Never by prefix: the `.*`-suffixed `ctrl\+x` form would declare a
            # composer with real text "ctrl+x does not help…" empty,
            # and the pilot would overwrite another input.
            tail = re.sub(TUI_HINT_ARROWS, "", tail).strip()
            if tail and any(re.fullmatch(phrase, tail, re.IGNORECASE)
                            for phrase in TUI_HINT_PHRASES):
                return True
            return len(tail) <= 2
    return False


def composer_overflowed(lines: list) -> bool:
    """Whether long input collapses into "↑ N more lines".

    Delivery checks look for the TAIL of the typed text in the composer line. When
    the text outgrows the visible area, the TUI collapses it, the tail leaves the
    screen — and the check falsely refuses text that stands on screen.

    Observed live: 4259 delivery chars landed in the neighbour composer, the
    composer showed "↑ 14 more lines", delivery reported unconfirmed, the message
    duplicated into the mailbox — and the human saw the same thing twice.
    """
    line = composer_line(lines)
    if line is None:
        return False
    return bool(re.search(r"\u2191\s*\d+|more lines?", line))


def chunks(text: str, size: int = CHUNK_BYTES) -> list:
    """Cut on word edges, never tearing identifiers."""
    out, buf = [], ""
    for word in text.split():
        candidate = (buf + " " + word) if buf else word
        if len(candidate.encode()) > size and buf:
            out.append(buf)
            buf = word
        else:
            buf = candidate
    if buf:
        out.append(buf)
    return out


def type_text(tmux: str, pane: str, text: str) -> None:
    """Type in chunks, restoring the separator that chunking consumed.

    chunks() joins words with a space and drops it at every boundary, so sending the
    pieces back to back loses one space per chunk: "must be found" arrived as
    "mustbefound". Harmless in prose, corrupting in a patch, so put it back.
    """
    parts = chunks(text)
    for index, chunk in enumerate(parts):
        piece = chunk + " " if index < len(parts) - 1 else chunk
        _send_keys(tmux, pane, "-l", piece)
        time.sleep(0.04)


def erase(tmux: str, pane: str, count: int) -> None:
    if count <= 0:
        return
    r = _send_keys(tmux, pane, "-N", str(count), "BSpace")
    if r.returncode != 0:
        batch = 50
        remaining = count
        while remaining > 0:
            cur = min(remaining, batch)
            _send_keys(tmux, pane, *(["BSpace"] * cur))
            remaining -= cur
            time.sleep(0.01)


def tail_present(lines: list, needle: str) -> bool:
    """Did the typed text actually appear on screen?

    The check reads the HEAD, not the tail — no renaming for beauty. Measured in
    our own composer: over-screen text draws from the start, while everything below
    leaves past the bottom edge and never reaches `capture` at all. A tail check for
    such text can never fire — and never fired, so every long-message delivery
    reported unconfirmed while the text landed and the neighbour ran it.

    The old version sought the tail for another reason: the cursor sits at the
    bottom while typed text stays on top, so "last lines" means empty. But no
    "start" was sought there either — simply unneeded while texts ran short.

    The start checks because it alone is guaranteed visible. The tail also serves
    as an extra mark: when the whole text fits, both show, and a tail match confirms
    delivery stronger.
    """
    meaningful = [line for line in lines if line.strip()]
    joined = " ".join(" ".join(meaningful).split())
    head = " ".join(needle[:60].split())
    tail = " ".join(needle[-60:].split())
    if head and head in joined:
        return True
    return bool(tail) and tail in joined


PERMISSION_PATTERNS = (
    r"\[[yY]/[nN]\]",
    r"\([yY]/[nN]\)",
    r"\[[yY]/[nN]/[a-zA-Z]+\]",
    r"\[yes/no\]",
    r"\b(?:Allow|Approve)\s+(?:execution\b|tool\b|bash\b|command\b|this\b)",
    r"\bDo you want to proceed\?",
    r"\bDo you allow this tool call\?",
)

# Prompt menu: the TUI shows no "[y/N]" but an option list with a select-and-
# confirm hint. Different keys there, and "y" never means agreement.
PERMISSION_MENU_MARKERS = (
    r"allow\s+once",
    r"always\s+allow",
    r"enter\s+confirm",
    r"(?:⇆|⇄|\btab\b)\s*select",
)
PERMISSION_HEADER = re.compile(r"permission\s+(?:required|needed)", re.IGNORECASE)

# The menu item allowing the op, with its digit. Two live menu kinds in real
# TUIs: horizontal (`Allow once · Always allow · Reject` with `⇆ select`) — Enter
# presses the selected item there — and numbered (`1) Allow once  2) Always allow
#  3) Deny`) — Enter presses the DEFAULT there, often a refusal. The digit sends
# only when the item explicitly allows: `Allow once` beats `Always allow` — least
# privilege.
MENU_ALLOW_ITEM = re.compile(
    r"^\s*(\d+)\s*[).:\]]\s*(Allow\s+once|Always\s+allow|Allow|Yes)\b", re.IGNORECASE)


def menu_allow_key(lines: list) -> str | None:
    """The digit of the allowing item in a numbered menu, or None.

    None answers too: the menu runs unnumbered (horizontal), and Enter presses the
    selected item as before. Guessing the digit is banned: the `Deny` item numbers
    just like `Allow`.
    """
    once: str | None = None
    always: str | None = None
    for line in lines:
        match = MENU_ALLOW_ITEM.match(line)
        if not match:
            continue
        digit, label = match.group(1), match.group(2).lower()
        if label == "allow once" or label == "yes":
            return digit
        if "always" in label and always is None:
            always = digit
    return once or always


def detect_permission_menu(lines: list) -> str | None:
    """A prompt shaped as a menu, not "y/N". Returns the header line or None.

    A separate function instead of widening detect_permission_prompt: that one reads
    only the last two nonempty lines, while in a menu the "Permission required"
    header stands six lines above the bottom ones. Widening its window would catch
    that line in ordinary output, where it allows nothing.

    Both the header and the menu marks are needed: one mark alone is no prompt yet,
    and one header alone is an ordinary log.
    """
    text = [line.strip() for line in lines if line.strip()]
    joined = "\n".join(text)
    if not PERMISSION_HEADER.search(joined):
        return None
    if not any(re.search(marker, joined, re.IGNORECASE) for marker in PERMISSION_MENU_MARKERS):
        return None
    for line in reversed(text):
        if PERMISSION_HEADER.search(line):
            return line
    return joined


def extract_permission_context(lines: list, prompt: str) -> str:
    """The text the verdict reads.

    A menu needs the whole screen: the command showing as `$ git status` lives in
    the modal body, not the header. On the header alone ("Permission required") the
    allowlist allows nothing and always refuses.

    For "y/N" the question line alone used to suffice, and on Claude Code that was
    a bug: the question runs last there while the command runs second to last, so
    the verdict read a line with zero words about the command and refused 100% of
    safe commands. Auto mode on Claude Code simply never worked.

    So both the question line and the lines above read — but never the whole screen:
    extra text could allow on a word unrelated to the command. The top edge is
    OVERSCAN_LINES lines, an open decision: agy asked two, three or till the
    separator, and I took three with margin for the frame between command and question.
    """
    text = [line.strip() for line in lines if line.strip()]
    joined = "\n".join(text)
    if any(re.search(marker, joined, re.IGNORECASE) for marker in PERMISSION_MENU_MARKERS):
        return " ".join(joined.split())
    tail = [line for line in text if line.strip()][-OVERSCAN_LINES:]
    context = " ".join(" ".join(tail).split())
    return context or prompt


def detect_permission_prompt(lines: list) -> str | None:
    """Return the matched prompt text if the terminal pane is showing an interactive confirmation prompt, else None."""
    meaningful = [line.strip() for line in lines if line.strip()]
    if not meaningful:
        return None
    for line in meaningful[-2:]:
        for pat in PERMISSION_PATTERNS:
            if re.search(pat, line, re.IGNORECASE):
                return line
    return None


# The six PROTOCOL.md §12 stop categories plus everything changing state outside
# the repo. A match on any of them bans self-confirmation under any allowlist:
# the human decision never yields to a tool setting.
NEVER_ALLOW_PATTERNS = (
    r"\.env\b",
    r"\bsecrets?\b",
    r"\btokens?\b",
    r"\bapi[_ -]?keys?\b",
    r"\bpasswords?\b",
    r"\bcredentials?\b",
    # Reading steals too. `cat` allowlists as reading, so the secret path itself
    # must ban: otherwise "read the key" counts as allowed reading.
    # that counts as allowed reading.
    r"\.ssh\b",
    r"id_rsa|id_dsa|id_ecdsa|id_ed25519",
    r"/etc/(?:shadow|passwd|sudoers)",
    r"\.(?:pem|p12|pfx|key)\b",
    r"\.npmrc\b",
    r"\.aws\b",
    r"\.git-credentials\b",
    r"\.kube/config",
    # cp/mv/ln assignments past the project: writing where the agent must never
    # write. The first arg (source) already binds allow-patterns, while the second
    # (target) nobody checked: `cp test.txt /etc/passwd` passed allow. Exceptions —
    # /tmp (own temp files, same edge as touch/mkdir/printf) and /dev/null.
    # A bare /tmp, /tmp/* and rm -rf / stay refuse (see NEVER above).
    r"\b(?:cp|mv|ln)\b[^\n]*?\s/(?!tmp/|dev/null\b)",
    # A pipe into a shell turns any read into execution.
    r"\|\s*(?:sudo\s+)?(?:ba|z|k|da)?sh\b",
    # Code handed to an interpreter as a string is arbitrary execution, not a check.
    # That is exactly how the allowlist bypass looked:
    # `python3 -c "import urllib.request; urllib.request.urlopen('http://x')" # test`
    # The word "test" in a comment satisfied the allow pattern, while `urllib` never
    # stood in the banned categories — network through it went uncaught.
    r"\b(?:python3?|node|ruby|perl)\s+-c\b",
    r"\$\(",
    r"`",
    r"\bfind\b[^\n]*-(?:delete|exec|execdir|ok)\b",
    r"\bxargs\b",
    r":\(\)\s*\{",
    r">\s*/dev/(?!null\b)",
    r"\bmkfs\b",
    r"\bdd\s+if=",
    r"\bdeploy\b",
    r"\bkubectl\b",
    r"\bhelm\b",
    r"\bdocker\b",
    r"\bssh\b",
    r"\bsudo\b",
    r"\bterraform\b",
    r"\bmigrat(?:e|ion)\b",
    r"\bdrop\s+(?:table|database)\b",
    r"\btruncate\b",
    r"\bdelete\s+(?:from|all)\b",
    # §10: never push into main. Pushing a feature branch and opening a PR is protocol
    # work, so banned is not push at large but push into main/master plus history
    # rewrites.
    r"\bgit\s+push\b[^\n]*\b(?:main|master)\b",
    r"\bgit\s+push\b[^\n]*--force\b",
    r"\bgit\s+reset\s+--hard\b",
    r"\bgit\s+clean\b[^\n]*-[a-z]*f",
    r"\bgit\s+filter-branch\b",
    r"\bgit\s+reflog\s+expire\b",
    r"\bgit\s+rebase\b[^\n]*\b(?:main|master)\b",
    # System destruction. Inside the project rm -rf build/ is routine cleanup, never
    # bannable next to rm -rf /: the difference sits in the target, not the command.
    # /tmp means named single-level subpaths with no globs (own temp cleanup): the cut
    # below narrows the guard to exactly them; bare /tmp, /tmp/* and rm -rf / stay
    # refuse, covered by tests.
    r"\brm\s+-[a-zA-Z]*[rf][a-zA-Z]*\s+(?:~|\$HOME|\*|\.\s*$|\.\.)",
    r"\brm\s+-[a-zA-Z]*[rf][a-zA-Z]*\s+/(?!tmp/[A-Za-z0-9_.-]+(?:\s|$))",
    r"\brm\s+-[a-zA-Z]*[rf][a-zA-Z]*\s+/tmp(?:\s|$|/\*|/\.?$)",
    r"\brm\s+-[a-zA-Z]*[rf][a-zA-Z]*\s+/tmp/[A-Za-z0-9_.-]+/.+",
    r"\brmdir\b",
    r"\bkill(?:all)?\b",
    r"\bchmod\s+777\b",
    r"\bprod(?:uction)?\b",
    r":\d{2,5}\b",
    # Network. Not because the network is dangerous alone, but because it carries out
    # what §12 guards and downloads what nobody checked.
    r"\bcurl\b",
    r"\bwget\b",
    r"\bnc\b\s+-",
    # Package installs drag foreign code into the project and change the operator env.
    r"\bpip[23]?\s+install\b",
    r"\bnpm\s+install\b",
    # Running tests on the host means arbitrary code with operator rights (P0).
    # Tests run isolated through scripts/run-isolated-test.sh (a container with no
    # network and no $HOME); a direct host run never self-confirms — human only. The
    # pattern deliberately runs wide: `pytest -q`, `python3 -m pytest`, `bun test`, `cargo test`.
    # Honest edge: a direct `python3 path/to/test_foo.py` (word not at token end) escapes
    # this — the interpreter allow-pattern covers it. A full argv parse instead of regex
    # sits past the P0 scope, see THREAT_MODEL.md.
    # The check below in permission_verdict: the run-isolated-test.sh segment skips the
    # The check below in permission_verdict: the run-isolated-test.sh segment skips the
    # test NEVER-patterns (but not the rest: network, secrets, sudo still refuse inside).
    # sudo still refuses even inside the runner).
    r"\bpytest\b",
    r"\b(?:python3?|node|ruby)\s+(?:-\S+\s+)*(?:[^\s]*[_./-]?)(?:test|tests|pytest|unittest)\b",
    r"\b(?:jest|vitest|mocha|rspec|nox|tox|nose|unittest)\b",
    r"\b(?:cargo\s+test|go\s+test)\b",
    r"\b(?:bun\s+(?:test|run\s+test)|npm\s+(?:test|ci))\b",
    r"\bnpm\s+publish\b",
    # Env-prefix variables change the process behaviour without changing the command
    # itself: LD_PRELOAD swaps libraries, PYTHONPATH/NODE_PATH swap the code the
    # interpreter loads. `LD_PRELOAD=/tmp/x.so pytest` is no longer "own tests" but
    # another library inside the test process.
    # Closed list: PATH/HOME/TZ/LANG and alike never change behaviour past the
    # allow-check, and stay allowed.
    r"(?:^|[\s;&|])LD_PRELOAD\s*=",
    r"(?:^|[\s;&|])(?:PYTHONPATH|PYTHONHOME|NODE_PATH|RUBYLIB|PERL5LIB|LD_LIBRARY_PATH)\s*=",
)

# The default allowlist: reading plus local work inside the project.
#
# The edge runs where §10 and §12 draw it, not where strictest: §10 demands
# commits and PRs, so "reads only" paralyzes the pair, while §12 bans not "work"
# but concrete things — secrets, deploys, migrations, data and git-history
# deletion, prod. Banned stays banned under any allowlist (see NEVER_ALLOW_PATTERNS),
# while allowed here is the work the protocol must do with no human anyway.
# the protocol owes with no human anyway.
#
# What misses here and why: network calls (curl/wget) and `ssh`. "Allow network"
# is not about reading files but about carrying them out, while §12 on secrets
# grants no "reads only" exception.
DEFAULT_ALLOW_PATTERNS = (
    # reading
    r"\bRead\b",
    r"\bread_file\b",
    r"\bview\b",
    r"\bglob\b",
    r"\bgrep\b",
    r"\bsearch\b",
    r"\blist(?:_dir|ing)?\b",
    r"\bcat\b",
    r"\bhead\b",
    r"\btail\b",
    r"\bls\b",
    r"\bfind\b",
    # editing inside the project
    r"\bwrite\b",
    r"\bedit\b",
    r"\bcreate\b",
    r"\breplace\b",
    r"\bpatch\b",
    r"\bapply\b",
    r"\bformat\b",
    r"\blint\b",
    # local checks: tests and builds
    r"\b(?:pytest|unittest|nose|tox|nox|jest|vitest|mocha|rspec|cargo\s+test|go\s+test)\b",
    r"\b(?:ruff|flake8|pylint|mypy|pyright|eslint|prettier|tsc|black|isort)\b",
    # Interpreter with a check in the command: `python3 -m pytest`,
    # `python3 mcp/pair_mailbox/test_protocol.py`, `node --test`. The keyword must be
    # an ARGUMENT. The old `[^\n]*` let it surface from anywhere further down the
    # line, and the word "test" in a comment or inside a string constant sufficed —
    # see NEVER_ALLOW_PATTERNS on `-c`.
    r"\b(?:python3?|node|ruby)\s+(?:-\S+\s+)*(?:[^\s]*[_./-]?)(?:test|tests|pytest|unittest|lint|check|build)",
    r"\bmake\b",
    r"\bnpm\s+(?:run\s+\w+|test|ci)\b",
    # bun is the runtime omp and pi turn on (pane_current_command showed bun on pair
    # panes): bun test / bun run / bun x is the same work as pytest directly, without
    # which the agent stalled on the routine prompt.
    r"\bbun\s+(?:test|run|x)\b",
    r"\bbuild\b",
    # Our bin/ runners: pairup/pairpilot/pair-duo/pairchat/pairctl/pairwatch/pairdoctor/
    # pairboard. Relativity and `..` guard match scripts/.
    # Pair-prefixed names — all: one-responsibility tool family.
    r"\bbash\s+bin/(?!/)(?![^\s]*\.\.)pair[a-z-]*\b",
    # A direct runner launch: `bin/pairup` at the command start. `(?<!\S)` means only
    # space or line start before `bin/`: or `/usr/local/bin/pairup` matches as "our
    # runner" while a foreign path from outside the project.
    r"(?<!\S)bin/(?!/)(?![^\s]*\.\.)pair[a-z-]*\b",
    # Our bash gates: verify-project, file-size-gate, eye-of-sauron, comment-lint,
    # security-scan and *_test.sh. Relativity inside the pattern itself: `scripts/` with
    # no `/` or `..` ahead — an absolute path or a project escape never matches here,
    # not only in tests. Task seq 329: routine runs demanded manual Allow for the same
    # work as pytest directly.
    r"\bbash\s+scripts/(?!/)(?![^\s]*\.\.)[A-Za-z0-9_.-]+\.sh\b",
    # Observation with no write: state reading, never changing (§12 untouched).
    # Clean observation — no redirect (>), no -i, no -o: printf > /etc, sed -i /etc,
    # sort -o /etc writes outside the project, not observation.
    # awk means refuse (the python3 -c class: system()/pipe execute);
    # trap/export mean refuse (safety in the value, no match tells apart).
    r"\b(?:date|du|wc|sleep|mktemp|which|command|echo|uptime|uniq|cut|tr|diff)\b",
    r"\b(?:mkdir|touch)\s+(?:-[a-zA-Z]+\s+)*(?:(?![/~$*])(?!\.{1,2}(?:\s|$))[^\s-]+|/tmp/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*)",
    r"\b(?:printf|sort)\b(?![^\n]*\s*>+\s*/(?!tmp/|dev/null))(?![^\n]*\s-o\s*/(?!tmp/))(?![^\n]*>+/(?!tmp/|dev/null))",
    r"\bsed\b(?![^\n]*-i[^\n]*\s/(?!tmp/))(?![^\n]*-i/(?!tmp/))",
    r"\bgit\s+check-ignore\b",
    r"\bpython3\s+--version\b",
    # File ops inside the project and /tmp: cp/mv/ln, flagless file rm, rm -rf of
    # named single-level /tmp subpaths with no globs. Bare /tmp, /tmp/* and rm -rf /
    # stay refuse (see NEVER above).
    # touch/mkdir live above, in the observe scope (same edges).
    r"\b(?:cp|mv|ln)\s+(?:(?![/~$*])(?!\.{1,2}(?:\s|$))[^\s]+|/tmp/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*)",
    r"\brm\s+(?:(?![/~$*./-])(?!\.{1,2}(?:\s|$))[^\s]+|/tmp/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*)",
    r"\brm\s+-[a-zA-Z]+\s+(?:(?![/~$*])(?!\.{1,2}(?:\s|$))[^\s]+|/tmp/[A-Za-z0-9_.-]+)",
    # local git: §10 demands a branch and a commit, so this is allowed;
    r"\bgit\s+(?:status|diff|log|show|branch|remote|describe|rev-parse|add|commit|"
    r"checkout|switch|stash|tag|blame|shortlog|reflog)\b",
    # shipping the work: push into a feature branch and PR. The ban on push into
    # main/master plus --force sits in NEVER_ALLOW_PATTERNS, checked first.
    r"\bgit\s+push\b",
    r"\bgh\s+(?:pr|issue|run|workflow)\b",
)


def load_allow_patterns(spec: str | None) -> tuple[list[str], bool]:
    """Read the permission allowlist: a JSON file or a pattern list.

    Returns (patterns, whether_we_ran_on_defaults).

    "Empty means confirm nothing" used to stand here, and the supervisor ended up
    confirming nothing at all: no `.pair/permissions.json` file exists, its path
    turned into the only pattern '.pair/permissions.json', never met the prompt
    text — and every request refused. From outside that read as "the neighbour
    stopped confirming".

    Now the edge is what the human set: inside the project everything the work
    needs is allowed; what harms the system, another project or wipes irreversibly
    is banned. Exactly DEFAULT_ALLOW_PATTERNS plus NEVER_ALLOW_PATTERNS on top,
    checked first. A missing file means "run on the default list", never "confirm
    nothing"; the file still narrows and widens.

    An explicit empty list is a deliberate self-confirm refusal, distinct from a
    missing file. An unreadable file yields empty: a broken setting must never
    silently widen rights.
    """
    if spec is None:
        return list(DEFAULT_ALLOW_PATTERNS), True
    if not str(spec).strip():
        # An empty line is a deliberate self-confirm refusal, and it differs from a
        # missing file. The difference shows only in this function, so tell apart here,
        # not in the caller.
        return [], False
    text = str(spec).strip()
    try:
        candidate = Path(text).expanduser()
    except (OSError, ValueError):
        return [], True
    if candidate.is_file():
        try:
            raw: object = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # The file exists and is broken. Silently taking defaults is banned: the operator
            # configured something explicitly, and quietly rolling its setting back to default beats
            # stopping and telling for worse.
            return [], False
    else:
        # A comma-less line with no existing file. It used to become the whole pattern,
        # silently breaking self-confirm; now it reads as a one-item list only when the
        # line holds commas.
        parts = [part.strip() for part in text.split(",") if part.strip()]
        # A path to a missing file also means "no settings", not a pattern. It used to
        # become the only pattern ('.pair/permissions.json') and never met the prompt text —
        # self-confirm never worked. A comma list is a real setting and reads as a list.
        # never worked. A comma list is a real setting and reads as a list.
        if len(parts) == 1 and ("/" in parts[0] or "\\" in parts[0]):
            return list(DEFAULT_ALLOW_PATTERNS), True
        return parts, False
    if isinstance(raw, dict):
        raw = raw.get("allow") or []
    if not isinstance(raw, list):
        return [], False
    return [str(item) for item in raw if str(item).strip()], False


def split_command_segments(prompt: str) -> list[str]:
    """Split the line into commands on shell separators outside quotes.

    Parsed via shlex in punctuation_chars mode: `;`, `&&`, `||`, `|` and `&` split
    into standalone tokens even without spaces (`pytest;rm`, `a||b`), untouched
    inside quotes (`echo \"a && b\"` is one segment). Redirects (`>`, `2>`, `>>`)
    stay inside the segment: they start no new command but belong to the current
    one. A line that never parses (an unclosed quote) returns whole as one segment —
    the unparsed never confirms indirectly; line matching parses it.
    """
    try:
        lexer = shlex.shlex(prompt, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return [prompt]
    separators = {";", "&&", "||", "|", "&"}
    segments: list[str] = []
    current: list[str] = []
    for token in tokens:
        if token in separators:
            segments.append(" ".join(current))
            current = []
        else:
            current.append(token)
    segments.append(" ".join(current))
    return [seg for seg in segments if seg]


# Host test-runner patterns (P0): never applied to the segment running through
# the isolated runner — the command executes in a container with no network and
# no $HOME, not with operator rights. The rest of NEVER (network, secrets, sudo)
# still holds there: the container never cures `curl x | sh` inside a test command.
# holds there too: the container never cures `curl x | sh` inside a test command.
TEST_RUNNER_NEVER = (
    r"\bpytest\b",
    # Interpreter with a test arg: `python3 -m pytest`, `node --test`. The keyword must
    # be an ARGUMENT (as in the allow pattern), or the word "test" in a comment
    # suffices. A direct `python3 path/to/test_*.py` escapes this pattern (word not at
    # token end) — the honest regex edge: a full argv parse sits past the P0 scope,
    # the limit pinned in THREAT_MODEL.
    r"\b(?:python3?|node|ruby)\s+(?:-\S+\s+)*(?:[^\s]*[_./-]?)(?:test|tests|pytest|unittest)\b",
    r"\b(?:jest|vitest|mocha|rspec|nox|tox|nose|unittest)\b",
    r"\b(?:cargo\s+test|go\s+test)\b",
    r"\b(?:bun\s+(?:test|run\s+test)|npm\s+(?:test|ci))\b",
)
ISOLATED_RUNNER = r"\brun-isolated-test\.sh\b"

def permission_verdict(prompt: str, allow: list[str] | None = None) -> dict:
    """Whether the supervisor may press "y" on this prompt with no human.

    Default-deny, and the ban checks before the allow. The two error prices never
    match: a hung agent costs one night, while a confirmed dangerous command
    destroys the unreturnable.

    A refusal returns with a cause: the human reads it, and an unexplained "no"
    never differs from a broken supervisor.

    The line splits into segments on shell separators outside quotes (see
    split_command_segments): every segment checks apart, and the verdict is the
    worst of the segments. A compound command confirms only when EVERY piece is
    safe: `pytest && echo ok` allows, while `pytest && curl x | sh` refuses. Quotes
    parse honestly: `echo \"a && b\"` is one segment, not two.

    Host test runs are banned (P0): a segment with a test runner refuses unless it
    rides the isolated runner (scripts/run-isolated-test.sh): there the command runs
    in a container with no network and no $HOME, not with operator rights. The rest
    of NEVER (network, secrets, sudo) holds inside the runner too: the container
    never cures `curl x | sh` inside a test command.
    """
    patterns = DEFAULT_ALLOW_PATTERNS if allow is None else tuple(allow)
    segments = split_command_segments(prompt)
    for segment in segments:
        isolated = re.search(ISOLATED_RUNNER, segment, re.IGNORECASE)
        for pattern in NEVER_ALLOW_PATTERNS:
            if pattern in TEST_RUNNER_NEVER and isolated:
                continue
            if re.search(pattern, segment, re.IGNORECASE):
                return {"verdict": "refuse", "matched": pattern,
                        "why": f"banned category (§12): {pattern}"}
    for segment in segments:
        matched_any = False
        for pattern in patterns:
            if re.search(pattern, segment, re.IGNORECASE):
                matched_any = True
                break
        if not matched_any:
            return {"verdict": "refuse", "matched": None,
                    "why": "not on the permission allowlist"}
    return {"verdict": "allow", "matched": None, "why": "on the allowlist"}
def resolve_permission_prompt(tmux: str, pane: str, allow: list[str] | None = None) -> dict:
    """Check if the pane is stalled waiting for a permission confirmation and confirm it.

    Returns a verdict dict: {approved, verdict, prompt, why, matched}. A refusal is
    a first-class outcome, not an error: the pane is still blocked, and somebody
    must be told that the supervisor deliberately left it that way.
    """
    idle = {"approved": False, "verdict": "none", "prompt": "", "why": "", "matched": None}
    if not pane_alive(tmux, pane):
        return {**idle, "why": "pane dead"}
    lines = capture(tmux, pane)
    menu = detect_permission_menu(lines)
    prompt = menu or detect_permission_prompt(lines)
    if not prompt:
        return idle

    if menu:
        # No input line lives in a menu, and "y" there means nothing: for this TUI "y"
        # never means agreement — confirmation is Enter on the selected item.
        # Nothing to check the composer for: none stands there at all, and that is fine.
        context = extract_permission_context(lines, prompt)
    else:
        line = composer_line(lines)
        # No composer means nobody to type to: silently confirming is banned.
        if line is None:
            return {**idle, "prompt": prompt, "verdict": "refuse",
                    "why": "no composer visible, nothing to confirm with"}
        if not detect_permission_prompt([line]) and not is_composer_empty(line):
            return {**idle, "prompt": prompt, "verdict": "refuse",
                    "why": "the input line holds its own text"}
        context = extract_permission_context(lines, prompt)

    decision = permission_verdict(context, allow)
    if decision["verdict"] != "allow":
        return {"approved": False, "verdict": "refuse", "prompt": prompt,
                "why": decision["why"], "matched": decision["matched"]}

    if menu:
        # Menus run two kinds. Horizontal (`⇆ select`): Enter presses the selected item,
        # with Allow once first, and that worked. Numbered (`1) Allow once`): Enter presses
        # the DEFAULT, possibly Deny: then an allowed op silently refused. The digit sends
        # only when the item explicitly allows and the verdict is allow already — see
        # menu_allow_key. No digit means the old behaviour, no guessing.
        # menu_allow_key. Without a digit the old behaviour holds, no guessing.
        key = menu_allow_key(lines)
        keys = [key, "Enter"] if key else ["Enter"]
        for key in keys:
            _send_keys(tmux, pane, key)
    else:
        _send_keys(tmux, pane, "y")
        _send_keys(tmux, pane, "Enter")
    return {"approved": True, "verdict": "allow", "prompt": prompt,
            "why": decision["why"], "matched": decision["matched"],
            "menu": bool(menu)}


def submit_verified(tmux: str, pane: str, body: str) -> str:
    """Press Enter and prove the composer took it: 'submitted' or 'retained'.

    Round 97 fixed the rc-trust in _ring (retained prompt after rc=0); the
    pilot path (deliver) sent Enter blind. One predicate for both: Enter,
    capture, retry once, capture again. Retained means the text still sits in
    the composer — the caller must say so durably, never claim submitted.
    """
    _send_keys(tmux, pane, "Enter")
    if not tail_present(capture(tmux, pane), body):
        return "submitted"
    _send_keys(tmux, pane, "Enter")
    return "retained" if tail_present(capture(tmux, pane), body) else "submitted"

def deliver(pane: str, text: str, wait_for_empty: float = 0.0, submit: bool = True) -> dict:
    """Deliver text into the neighbour composer. Returns a report of what happened.

    Raises ComposerBusy when the composer is busy — typing over another text is
    banned: it loses what the human types.
    """
    tmux = _tmux()
    if not pane_alive(tmux, pane):
        raise ComposerBusy("pane dead or missing")

    deadline = time.monotonic() + max(0.0, wait_for_empty)
    while True:
        line = composer_line(capture(tmux, pane))
        # None means no composer visible at all. Not "free": nowhere to type, and keys
        # past the composer never send.
        if line is not None and is_composer_empty(line):
            break
        if time.monotonic() >= deadline:
            seen = line.strip()[-60:] if line else "no composer visible"
            raise ComposerBusy("composer busy: " + (seen or "?"))
        time.sleep(0.5)

    body = " ".join(text.split())
    type_text(tmux, pane, body)

    # The paste check over several reads, not one.
    landed = False
    for attempt in range(VERIFY_ATTEMPTS):
        if tail_present(capture(tmux, pane), body):
            landed = True
            break
        if attempt + 1 < VERIFY_ATTEMPTS:
            time.sleep(VERIFY_PAUSE)
    if composer_overflowed(capture(tmux, pane)):
        # Text on screen, just the tail collapsed. Calling it "undelivered" here is banned:
        # the double trace — a mailbox message AND composer text at once — is exactly what
        # confuses the human.
        result = {"delivered": True, "chars": len(body),
                  "chunks": len(chunks(body)), "pane": pane, "submitted": submit,
                  "tail_hidden": True}
        if submit:
            submit_state = submit_verified(tmux, pane, body)
            result["submit_state"] = submit_state
            if submit_state == "retained":
                result["why"] = ("Enter sent twice, the text still sits in the composer — "
                                 "take the message from the mailbox")
        return result
    if not landed:
        # Unconfirmed means UNKNOWN whether the text landed, not "it never did".
        # Blind erasing is banned: erase(len(body)) never knows whose input it is, eating
        # the neighbour or human text typed in the same pane. The old rollback on one stale
        # frame did exactly that — reporting "rolled back" over text lying on screen.
        # frame did exactly that — reporting rolled-back over text lying on screen.
        #
        # The error price runs asymmetric: an extra mailbox message costs one glance, while
        # a wiped neighbour input costs its work. So doubt travels on, and the caller leaves
        # for the mailbox, where the message is surely read.
        return {"delivered": False, "unverified": True, "chars": len(body),
                "pane": pane, "submitted": submit,
                "why": "the paste never confirmed on screen; the composer may keep text, "
                       "never rolling back blind — take the message from the mailbox"}

    result = {"delivered": True, "chars": len(body), "chunks": len(chunks(body)),
              "pane": pane, "submitted": submit}
    if submit:
        submit_state = submit_verified(tmux, pane, body)
        result["submit_state"] = submit_state
        if submit_state == "retained":
            result["why"] = ("Enter sent twice, the text still sits in the composer — "
                             "take the message from the mailbox")
    return result
