#!/usr/bin/env python3
"""pair-mailbox: a transport and protocol boundary for two independent coding agents.

Two agents (OpenCode, Antigravity/agy, Codex, Claude Code, anything with an MCP
client) are configured as separate instances of this server, each with its own
``--role``. They share one state directory and use it to talk to each other.

Why this exists: every agent CLI now has multi-agent features, but none of them
can talk to a *different* vendor's CLI. This server is the only channel that
crosses that line, and it is also where the collaboration rules are enforced.

The rules are enforced here, in the server, rather than in prompts. A prompt is
advice; a refusal is a boundary.

Rules
-----
1. Exactly one role holds write authority at a time. Only the human transfers it,
   through ``pairctl``. A peer message can never grant, revoke or move it.
2. A non-writer never edits project files. It reserves a patch file, fills it,
   seals it, and sends the path plus a SHA-256. The writer copies it once and
   re-verifies the hash immediately before applying.
3. A patch is only ever released by exact path. Glob patterns are rejected.
4. "The peer agreed" is not an approval. The server records agreement; only the
   human authorises.
5. Every action lands in an append-only transcript, so a run can be audited
   afterwards.

Transport: MCP over stdio, newline-delimited JSON-RPC 2.0, no dependencies.
Human control surface: ``bin/pairctl <command>`` (execs ``server.py --state <dir> <command>``).
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# composer.py sits nearby and imports lazily, inside pair_deliver.
# The path joins here, once Path exists.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import digest  # noqa: E402 - needs sys.path above
import liveness  # noqa: E402 - needs sys.path above
import obligations  # noqa: E402 - needs sys.path above
import restart_trace  # noqa: E402 - needs sys.path above
import server_freshness  # noqa: E402 - needs sys.path above
import statecache  # noqa: E402 - needs sys.path above
import read_delivery  # noqa: E402 - pending mailbox batch transitions
from read_wait import (  # noqa: E402 - reads the role's configured MCP budget
    DEFAULT_READ_WAIT_SECONDS, MAX_READ_WAIT_SECONDS, client_timeout_ms, effective_read_wait,
)

# Digest names re-export from `digest`: `pairctl_read` reads them, and keeping a
# second source of truth means returning the drift the digest was extracted for.
# extracted for.
DIGEST_TOKEN_BUDGET = digest.DIGEST_TOKEN_BUDGET
DIGEST_MARKER = digest.DIGEST_MARKER
TRANSCRIPT_SCAN_LIMIT = digest.TRANSCRIPT_SCAN_LIMIT
build_digest = digest.build_digest
estimate_tokens = digest.estimate_tokens

# The task banner for panes is its own module: server.py is frozen
# (file-size-baseline.tsv), and monolith growth is banned without extraction.
from task_banner import write_task_banner
_digest_marker_reserve = digest._digest_marker_reserve
_render_digest = digest._render_digest
_digest_line = digest._digest_line
DIGEST_MIN_LINE = digest.DIGEST_MIN_LINE

# MCP protocol revisions this server understands. We echo the client's requested
# revision back when we know it, because clients reject a downgrade they did not
# ask for.
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_PROTOCOL_VERSION = "2024-11-05"

SERVER_NAME = "pair-mailbox"
SERVER_VERSION = "1.0.0"

GLOB_CHARS = ("*", "?", "[", "]")

# Phase graph. Mirrors the shape that works in practice: scope the work, agree on
# a plan, write it, then verify against the real code before calling it done.
PHASES = ("scoping", "plan", "implement", "verify", "done")
PHASE_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "scoping": ("plan",),
    "plan": ("scoping", "implement"),
    "implement": ("plan", "verify"),
    "verify": ("implement", "done"),
    "done": ("implement",),
}

# One step back belongs to any role, not only the writer. This is the critic's
# veto: a round cannot be closed on top of a finding nobody reported yet, and
# reopening a round is not a write operation.
PHASE_BACKWARD: dict[str, str] = {
    "plan": "scoping",
    "implement": "plan",
    "verify": "implement",
    "done": "implement",
}

# Round modes. `fix` is the default and everything that existed before: the writer
# edits project files, the critic proposes sealed patches, git ends in a PR.
#
# `audit` is a round where nothing is changed on purpose. The pair still does real
# work — find defects, try to refute each other's findings, park what will not be
# fixed — but the deliverable is evidence, not a diff. The distinction lives in
# shared state rather than in the task text, because every agent gets its standing
# instructions from state and prompt, and those two must not contradict the task.
MODES = ("fix", "audit")

# How much the pair runs without the human. This is deliberately a SEPARATE axis
# from `mode`: mode says what a round is for, autonomy says how far it may go on
# its own. Folding them together would make `pairctl mode auto` silently turn an
# audit round back into a code round, because every audit check tests `mode ==
# "audit"`.
#
# - observe: the supervisor only speaks. It does not restart a dead agent and
#   does not confirm a permission prompt. The human is at the wheel.
# - semi (default): the supervisor restarts a dead agent and confirms prompts on
#   the allowlist, and closing a round needs evidence plus one word from the human.
# - auto: the same, and the round may close itself once the evidence is there.
#
# §12 is outside this axis entirely: secrets, ports, deploys, migrations, deleting
# data or git history, and production stop the pair in every mode.
AUTONOMIES = ("observe", "semi", "auto")

# What may be recorded as proof that the round did something. Every kind here is
# something a machine can produce on its own; "the peer seemed happy" is not one
# of them, which is the whole point.
EVIDENCE_KINDS = ("test", "lint", "typecheck", "build", "diff", "commit", "pr", "manual",
                  "ground-truth")

EVIDENCE_KEEP = 50

# Notes write append-only, and with no ceiling they grow until reading them whole
# stops making sense. Measured live: verdict.md grew to 630 lines, about 19 thousand
# tokens per ONE read. The critic reads notes with a file tool, because `pair_note`
# never owned a second side — reading — and no bounded form existed at all.
# existed at all.
# Rotation carries the old into archive, leaving a pointer in the file: nothing is
# lost, and the file stops growing without bound.
NOTE_LINE_CAP = 600
NOTE_READ_DEFAULT = 120
# The transcript sliding window. The journal stays on disk whole, but the active
# file is capped: without it `read_transcript` parsed the whole file for twenty reply
# lines, and limitless `pairctl log` dumped into the terminal everything the round
# gathered. The value runs with margin above a typical round, so rotation never fires
# on every second call.
TRANSCRIPT_MAX_BYTES = 256 * 1024
# How many chunks how much past to keep before releasing the oldest.
TRANSCRIPT_ARCHIVES = 4
# A read portion from the end. Below a block means more syscalls, above means more
# memory; 64 KB reads faster than a journal page without dragging it whole.
TRANSCRIPT_READ_BLOCK = 64 * 1024
# Limitless `pairctl log` serves this much, not the whole file: the same spend as
# `pair_transcript`, only with no limiter.
LOG_DEFAULT_LIMIT = 50
NOTE_READ_MAX = 400

# Phases where the human "ok" word means anything. Only the round number checked
# before, and a plan approval closed the round on verify.
OK_PHASES = ("verify", "done")

# Auto-release is off unless the human turns it on. A writer that has finished
# and gone quiet holds authority in no useful sense.
STALL_SECONDS = int(os.environ.get("PAIR_STALL_SECONDS", "0") or 0)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(message: str) -> None:
    """Diagnostics go to stderr. stdout carries the protocol and nothing else."""
    print(f"[{SERVER_NAME}] {message}", file=sys.stderr, flush=True)


def global_roles_path() -> Path:
    """Where the human's across-every-project role pinning lives."""
    return Path(os.environ.get("PAIR_ROLES", Path.home() / ".config" / "pair-mailbox" / "roles.json"))


def permissions_path(root: Path) -> Path:
    """Where the human keeps the list of prompts a supervisor may confirm."""
    return Path(os.environ.get("PAIR_PERMISSIONS", root / "permissions.json"))


def load_allowlist(root: Path) -> list[str]:
    """Allowed patterns and the flag that we run on the default list.

    A missing file used to mean an empty list — "confirm nothing" — and the
    supervisor confirmed nothing at all — while outside it read as neighbour
    silence, not a settings refusal. The edge is human-set: inside the project
    everything the work needs is allowed; what harms the system or another
    project is banned. So no file means the default list.

    A broken file differs: the operator configured something, and silently
    swapping the default for its setting beats stopping.
    """
    from composer import load_allow_patterns

    path = permissions_path(root)
    try:
        is_file = path.is_file()
    except OSError:
        is_file = False
    if not is_file:
        from composer import DEFAULT_ALLOW_PATTERNS
        return list(DEFAULT_ALLOW_PATTERNS), True
    try:
        return load_allow_patterns(str(path))
    except Exception:  # noqa: BLE001 - a broken setting must never drop the server
        return [], False


def autonomy_of(state: dict[str, Any]) -> str:
    """Read the autonomy level, refusing to invent one.

    An unknown value is treated as `observe`, the level that does the least: a
    state written by a newer build must never hand this build more authority than
    the human asked for.

    A MISSING value reads as `auto`, because that is the level the operator set as
    the default: he asked for the pair to run the round end to end and to come back
    with nothing but the result. An absent key is not "the human asked for caution",
    it is an older state file, and reading it as `semi` meant the supervisor stopped
    at `verify` to ask for a word nobody intended to withhold.
    """
    value = state.get("autonomy", "auto")
    return value if value in AUTONOMIES else "observe"


def autonomy_file_path(state_dir: Path) -> Path:
    """Where a project keeps its standing autonomy, one word, no punctuation."""
    return state_dir / "autonomy"


# A task may carry its own level on its first line: `autonomy: semi`. Anchored to the
# start so a word inside the prose cannot change how much authority the pair takes.
AUTONOMY_TASK_RE = re.compile(
    r"\A\s*autonomy\s*[:=]\s*(observe|semi|auto)\b", re.IGNORECASE,
)


def resolve_autonomy(
    state_dir: Path,
    task: str = "",
    flag: str | None = None,
) -> tuple[str, str]:
    """Decide the autonomy level and say where the decision came from.

    Four sources, strongest first: an explicit flag from the launcher, a line in the
    task, a file in the project, and the default. The source travels with the value
    into the transcript, because a level nobody can account for is a level that will
    be disputed later — "why was this round allowed to close itself?" is a question
    with no answer unless the reason was written down when it was decided.

    A malformed file or flag falls through to the next source instead of failing the
    launch. Refusing to start is a worse answer than starting with a level the
    operator can see in `pairctl status`.
    """
    if flag:
        level = flag.strip().lower()
        if level in AUTONOMIES:
            return level, "flag"

    match = AUTONOMY_TASK_RE.match(task or "")
    if match:
        return match.group(1).lower(), "task"

    try:
        if autonomy_file_path(state_dir).is_file():
            declared = autonomy_file_path(state_dir).read_text(
                encoding="utf-8", errors="replace"
            ).strip().strip(":=").strip().lower()
            if declared in AUTONOMIES:
                return declared, "file"
    except OSError:
        pass  # an unreadable file is a fall-through, not a launch failure

    return "auto", "default"


# The three helpers below are plain functions of the state, not methods, because
# two processes need to agree on them: the server decides whether a round may
# close, and bin/pairpilot has to tell the human the same thing in the same words.
# A second copy of this rule would eventually disagree with the first, and the
# human would be shown "all good" by one side and refused by the other.


def round_evidence(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Evidence recorded for the round that is running right now.

    Scoped to the round on purpose. Evidence is not a trophy the pair keeps: what
    proved round 3 says nothing about round 7, and carrying it forward would let a
    later round be closed by an earlier round's tests.
    """
    current = int(state.get("round", 0))
    return [item for item in (state.get("evidence") or []) if item.get("round") == current]


OPEN_TASK = "open"
TASK_STATUSES = (OPEN_TASK, "done", "dropped")


def tasks_of(state: dict[str, Any]) -> list[dict[str, Any]]:
    return list(state.get("tasks") or [])


def open_tasks(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in tasks_of(state) if item.get("status") == OPEN_TASK]


def next_task_id(state: dict[str, Any]) -> int:
    return max((int(item.get("id", 0)) for item in tasks_of(state)), default=0) + 1


def _verdict_owes_review(text: str) -> bool:
    """Does this verdict still leave the writer work to answer?

    Round 104 S3. `kind=verdict*` used to mean yes by itself, so a critic's FINAL
    VERDICT — approve, or a plain review-ack — opened a review obligation on the
    writer anyway. Live cost (peer, rounds 103/104): the approve left
    pending_reviews=1 and the round could not close until the writer posted
    ANOTHER note; the queue held a completed round hostage over a review that
    was already done.

    The signal is the content, and it is deliberately conservative: a
    needs-work / objection / reopen phrased anywhere opens the queue; approve,
    accepted and ack do not. When in doubt it opens — a missed real request
    blocking a round is the failure the protocol prefers over a silent pass,
    and the close gate refuses either way until the work is genuinely done.
    """
    low = (text or "").lower()
    if any(marker in low for marker in (
        "verdict-approve", "verdict approve", "accepted", "approved",
        "no objection", "no open items", "acknowledged",
    )):
        return False
    return any(marker in low for marker in (
        "needs work", "needs-work", "reopen", "re-open", "hold s", "refus",
        "reject", "objection", "i object", "must be fixed", "fix this",
        "re-review", "rereview", "redo",
    ))


def _subject(state: dict[str, Any]) -> str:
    """The round's subject — task_subject.py: one rule, four surfaces."""
    from task_subject import subject_line as impl

    return impl(state)

def human_ok_for_round(state: dict[str, Any]) -> bool:
    """The human word at the checkpoint — and only where it matters.

    Only the round number checked before, so "ok: approving the plan" on phase
    plan closed the round on verify. Plan and "edits checked" are different
    claims, and the gate never told them apart.

    Now only ok counts, said on verify or done, and only while the round
    stands on that phase. A phase rollback cancels the word on its own: the
    approve bound to what the human saw.
    """
    current = int(state.get("round", 0))
    if state.get("phase") not in OK_PHASES:
        # A round rolled back — what was said on verify belonged to what the human saw
        # then, and after the rollback it never saw it again.
        return False
    entries = [entry for entry in (state.get("human_ok") or [])
               if int(entry.get("round", -1)) == current]
    if not entries:
        return False
    # The last word decides: a revoke cancels the earlier approval, and a repeated
    # "ok" after a revoke is a new word.
    last = entries[-1]
    if last.get("revoke"):
        return False
    # And it must be said at a checkpoint that means something: "plan approved" and
    # "edits checked" are different claims.
    return last.get("phase") in OK_PHASES


def unresolved_patches(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Sealed patches that nobody has recorded as applied or released.

    `applied` is set to False at reserve and flipped only by `pair_patch_applied`.
    Before that verb existed there was nowhere to say whether a patch had reached
    the tree, so a round could close over a proposal that had been sitting sealed
    and untouched since the night before. That is the exact shape of work that
    looks finished and is not. A failed apply leaves `applied` False on purpose:
    a rejected hunk is not an answer, it is an open question.
    """
    out = []
    for path, record in (state.get("patches") or {}).items():
        if not record.get("sealed") or record.get("applied"):
            continue
        out.append({"path": path, "owner": record.get("owner"),
                    "sha256": record.get("sha256"),
                    "sealed_at": record.get("sealed_at"),
                    "verified_match": record.get("verified_match")})
    return out


def done_blockers(state: dict[str, Any]) -> list[str]:
    """Everything that must be true before a round may close, in plain words.

    The refusal message is the only thing the writer sees at that moment, so it
    has to say what is missing and what to call. A gate nobody can satisfy is
    indistinguishable from a broken server.
    """
    blockers: list[str] = []
    autonomy = autonomy_of(state)
    round_number = int(state.get("round", 0))

    # Per kind, the LAST run decides, not any of them: tests failed, got fixed,
    # ran again — the round may close. Otherwise the first failure would hold the
    # round open forever, because the journal only grows.
    latest: dict[Any, dict[str, Any]] = {}
    for item in round_evidence(state):
        latest[item.get("kind")] = item
    failed = [item for item in latest.values() if not item.get("ok")]
    passed = [item for item in latest.values() if item.get("ok")]
    if failed:
        names = ", ".join(f"{item.get('kind')}={item.get('value')!r}" for item in failed)
        blockers.append(
            f"round {round_number} ends on failing evidence ({names}). Fix it and record the "
            f"passing result, or say in a note why the failure does not block closing."
        )
    if not passed:
        blockers.append(
            f"round {round_number} has no passing evidence. Call pair_evidence with what you "
            f"actually ran: tests with the count and exit code, lint, typecheck, or the commit "
            f"and PR. Kinds: {', '.join(EVIDENCE_KINDS)}."
        )

    if state.get("mode", "fix") != "audit" and not any(
            item.get("kind") == "test" and item.get("ok")
            and item.get("role") == state.get("writer")
            for item in latest.values()):
        # The gate used to ask "any successful record at all", and `kind="manual",
        # value="looks good"` answered that question. A record indistinguishable from a
        # run is exactly what the gate must never close on: a round with red tests was
        # declared closed because the author described in words that all is well. In
        # mode=audit no requirement holds — the work result there is findings in notes,
        # and no test run may exist in substance.
        #
        # And the run must be the writer one. Otherwise the gate guarding against
        # "closing on words" closes on words too — just from another role: the critic may
        # record ok=True and satisfy the human gate, who never owns write authority.
        # owns write authority.
        writer = state.get("writer")
        blockers.append(
            f"round {round_number} has no passing test run from the writer "
            f"({writer or 'nobody holds authority'}), and mode=fix closes only on one. "
            f"The writer calls pair_evidence(kind=\"test\", value=\"181 passed, exit 0\") "
            f"with what the machine actually printed. kind=lint, typecheck or a commit hash "
            f"is not a substitute, another role's 'ok' is not a substitute either, and neither "
            f"is a verdict that things look right."
        )

    # [R82] With no PASS of a live writer gate run — never close (R81).
    if state.get("mode", "fix") != "audit" and not any(
            item.get("kind") == "ground-truth" and item.get("ok")
            and item.get("role") == state.get("writer")
            for item in latest.values()):
        blockers.append(
            f"round {round_number} has no passing ground-truth run from the writer. "
            f"Run session_facts + anti-mock gate + verify-ground-truth, then "
            f"pair_evidence(kind=\"ground-truth\", value=\"PASS ...\")."
        )

    if autonomy == "semi" and not human_ok_for_round(state):
        blockers.append(
            "autonomy is semi, so closing a round waits for the human. Send them the digest "
            "and, once they agree, run `bin/pairctl ok`. `bin/pairctl autonomy auto` closes "
            "without that."
        )

    for item in open_tasks(state):
        blockers.append(
            f"task #{item.get('id')} is still open: {str(item.get('text'))[:100]!r}. "
            f"A round closes only when nothing is left open: pair_task(action=\"done\", id={item.get('id')}) "
            f"with it finished, or pair_task(action=\"drop\", id={item.get('id')}, "
            f"reason=\"why not\") if it turned out not to be needed. Dropping needs a "
            f"reason on purpose — a task vanishes silently otherwise, which is the "
            f"failure this list exists to prevent."
        )

    if state.get("mode", "fix") != "audit":
        for item in unresolved_patches(state):
            blockers.append(
                f"patch {item['path']} (sha256 {item['sha256']}) was sealed by {item['owner']} "
                f"and never applied. Verify it with pair_patch_verify, then "
                f"pair_patch_applied(path, ok), or pair_patch_release if it was rejected."
            )
    return blockers


def render_tasks_md(state: dict[str, Any]) -> str:
    """The session journal: what is set, what is done, what proves it, what is NOT done.

    Generated from state, never hand-written, so it never stales and can never drift
    from facts. Lives in .pair, not docs: the pair work journal, not project
    docs — no point committing it to the public repo.

    The "not done" section never sits silently empty: an open task fills it
    automatically, while a closed one must carry a cause when dropped. Exactly
    what the human returning in a week needs.
    """
    items = tasks_of(state)
    mark = {OPEN_TASK: "NOT DONE", "done": "done", "dropped": "dropped"}
    lines = [
        "# Session journal",
        "",
        f"- round: {state.get('round')}   phase: {state.get('phase')}   "
        f"mode: {state.get('mode')}   autonomy: {autonomy_of(state)}",
        f"- writer: {state.get('writer') or 'unassigned'}",
        f"- generated: {utcnow()}",
        "",
    ]
    if not items:
        lines += ["No tasks. `pairctl task \"...\"` or pair_task(action=\"add\").", ""]
        return "\n".join(lines)

    still_open = [i for i in items if i.get("status") == OPEN_TASK]
    lines += [
        "## Summary",
        "",
        f"- total tasks: {len(items)}",
        f"- not done: {len(still_open)}"
        + (f" — #{', #'.join(str(i['id']) for i in still_open)}" if still_open else ""),
        "",
        "## Tasks",
        "",
    ]
    for item in items:
        ident = item.get("id")
        lines.append(f"### #{ident} — {mark.get(item.get('status'), item.get('status'))}")
        lines.append("")
        lines.append(str(item.get("text", "")).strip())
        lines.append("")
        lines.append(f"- added: {item.get('added_by')}   round: {item.get('round')}   "
                     f"in {item.get('added_at')}")
        if item.get("status") != OPEN_TASK:
            lines.append(f"- closed: {item.get('closed_by')}   in {item.get('closed_at')}")
        if item.get("status") == "dropped":
            reason = str(item.get("reason") or "").strip()
            lines.append(f"- why dropped: {reason or 'CAUSE MISSING — a defect'}")
        proved = [e for e in (state.get("evidence") or []) if e.get("task") == ident]
        if proved:
            lines.append("- proven by:")
            for entry in proved:
                flag = "OK " if entry.get("ok") else "FAIL"
                lines.append(f"    - {flag} {entry.get('kind')} [{entry.get('role')}] "
                             f"{entry.get('value')}")
        elif item.get("status") != OPEN_TASK:
            lines.append("- proven by: NOTHING. Closed with zero machine facts.")
        else:
            lines.append("- proven by: not yet, the work never finished.")
        notes = [e for e in (state.get("evidence") or [])
                 if e.get("task") == ident and not e.get("ok")]
        if notes:
            lines.append("- what failed:")
            for entry in notes:
                lines.append(f"    - {entry.get('value')}")
        lines.append("")

    if still_open:
        lines += [
            "## What stays behind the open tasks",
            "",
            "The round never closes until this is done or dropped with a cause.",
            "",
        ]
        for item in still_open:
            lines.append(f"- #{item.get('id')}: {str(item.get('text'))[:120]}")
        lines.append("")

    loose = [e for e in (state.get("evidence") or []) if not e.get("task")]
    if loose:
        lines += [
            "## Evidence with no task binding",
            "",
            "Recorded while more than one task stayed open, so never bound to any. "
            "To bind to a task explicitly: pair_evidence(..., task=N).",
            "",
        ]
        for entry in loose:
            flag = "OK " if entry.get("ok") else "FAIL"
            lines.append(f"- {flag} {entry.get('kind')} [{entry.get('role')}] {entry.get('value')} "
                         f"— open were: {', '.join(f'#{i}' for i in entry.get('tasks_open') or []) or '—'}")
        lines.append("")
    return "\n".join(lines)


def write_tasks_md(state_dir: Path, state: dict[str, Any]) -> Path:
    target = Path(state_dir) / "tasks.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_tasks_md(state), encoding="utf-8")
    return target


def read_global_policy() -> dict[str, Any] | None:
    """The pinning the human set for all projects at once.

    Read on every state load and only when the project has none of its own, so a
    project can always override it and never the other way round. An unreadable or
    nameless file means no policy, which leaves the race in place rather than
    guessing — a wrong writer is far worse than a slow one.
    """
    try:
        data = json.loads(global_roles_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(data, dict) and data.get("writer"):
        return data
    return None

CHAIN_GENESIS = "chain-genesis-v1"

class ProtocolError(Exception):
    """A request that is well-formed MCP but violates the collaboration rules."""

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint






def _transcript_hash(entry: dict) -> str:
    """The chain-link string hash: all but prev_hash, canonical json."""
    body = {k: v for k, v in entry.items() if k != "prev_hash"}
    return hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def verify_transcript_chain(entries: list[dict], anchored: bool = False) -> dict:
    """Verify the chain: every line points at the previous hash.
    Rotation is the verification horizon (critic condition seq 415): the active file
    starts with a transcript_rotated marker, the tail before it archived away —
    so the first line verifies from the marker, the archive apart by last_hash.
    The marker is an ordinary link past that (carries prev_hash like all) plus a
    last_hash checkpoint of the carried file. Damage lines are a visible break.
    A slice (anchored=True, round 42): tool_pair_transcript serves the tail limit,
    the slice first line is a mid-journal anchor with nothing to reconcile its
    prev_hash against; the chain inside the slice verifies. Swapping the slice
    first line still shows: its hash never meets the second line.
    Legacy (round 42): pre-chain lines with no prev_hash are no break but a
    pre-chain era; skipped with a pre_chain counter, the chain builds from the
    first signed line."""
    expected = None
    checked = 0
    pre_chain = 0
    for index, entry in enumerate(entries):
        if entry.get("_damaged"):
            return {"ok": False, "bad_at": index, "checked": checked, "pre_chain": pre_chain,
                    "reason": f"damaged line {index} breaks chain"}
        if entry.get("kind") == "transcript_rotated" and expected is None:
            expected = _transcript_hash(entry)
            checked += 1
            continue
        if "prev_hash" not in entry:
            # Pre-chain era: no signatures, nothing to check — but the chain builds on from
            # it, or the first signed line past a legacy head would always tear the chain
            # (fail-open, round 42).
            pre_chain += 1
            expected = _transcript_hash(entry)
            continue
        if anchored and index == 0 and expected is None:
            # The slice anchor: the first line from the journal middle, its prev_hash has
            # nothing to reconcile with — taken, the chain builds on from it.
            # An anchor swap still shows: its hash never meets the second line.
            expected = _transcript_hash(entry)
            checked += 1
            continue
        want = expected if expected is not None else CHAIN_GENESIS
        if entry.get("prev_hash") != want:
            return {"ok": False, "bad_at": index, "checked": checked, "pre_chain": pre_chain,
                    "reason": f"prev_hash mismatch at line {index}: chain broken"}
        expected = _transcript_hash(entry)
        checked += 1
    return {"ok": True, "checked": checked, "pre_chain": pre_chain}

def _transcript_entry(raw: str) -> dict | None:
    """One journal line into a dict, or None when no record.

    A broken line must never drop the read: the journal writes one line per record,
    and a mid-line cut is routine with a killed process. Skipping such a line quietly
    is right, but telling about it is right too — or it reads as if no event ever
    was. So `pairctl log` prints it marked, never dropped.
    """
    text = raw.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"_damaged": True, "_raw": text[:200]}


def _transcript_tail(path: Path, limit: int) -> list[dict]:
    """The last `limit` records, read from the file end.

    Grows from the block end, answering once enough lines gather. The offset seeks
    the line start: or the first caught line would be a stub of another record,
    and the answer would grow a record that never was.
    """
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        position = handle.tell()
        # `pending` is what is read but never met its line end.
        # In a file read from the end, only the HEAD of the stash can run partial —
        # hence it alone is held apart.
        pending = b""
        lines: list[bytes] = []
        while position > 0 and len(lines) <= limit:
            block = min(position, TRANSCRIPT_READ_BLOCK)
            position -= block
            handle.seek(position)
            pending = handle.read(block) + pending
            parts = pending.split(b"\n")
            pending = parts[0]
            # Empty chunks drop at once: a trailing newline yields one extra empty item,
            # taking tail room — minus one real record in the answer, so the "last 20" arrive
            # nineteen.
            # nineteen.
            lines = [line for line in parts[1:] if line.strip()] + lines
        if pending.strip():
            lines.insert(0, pending)
        # `lines` already run in file order: every next block reads earlier than the
        # previous, so it lands at the start. The answer is the last `limit` records in
        # chronological order, as on a straight read.
        out = []
        for raw in lines[-limit:]:
            entry = _transcript_entry(raw.decode("utf-8", "replace"))
            if entry is not None:
                out.append(entry)
            if len(out) >= limit:
                break
        return out


class Store:
    """Filesystem-backed shared state, guarded by an advisory lock.

    Two server processes with different roles share this directory, so every
    read-modify-write goes through the lock. Writes are atomic renames.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.mailbox_root = root / "mailbox"
        self.notes_root = root / "notes"
        self.patches_root = root / "patches"
        self.obligations_root = root / "obligations"
        self.notifications_root = root / "notifications"
        self.state_path = root / "state.json"
        self.transcript_path = root / "transcript.jsonl"
        self.lock_path = root / ".lock"
        for directory in (self.root, self.mailbox_root, self.notes_root,
                          self.patches_root, self.obligations_root,
                          self.notifications_root):
            directory.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _notification_role_dir(role: str) -> str:
        """Hash role for a path component; roles never become filesystem paths."""
        return hashlib.sha256(role.encode("utf-8")).hexdigest()[:16]

    def publish_resource_update(self, role: str, uri: str) -> None:
        """File-only cross-process hint. Mailbox remains the durable source of truth."""
        folder = self.notifications_root / self._notification_role_dir(role)
        folder.mkdir(parents=True, exist_ok=True)
        atomic_write_json(folder / f"{uuid.uuid4().hex}.json",
                          {"role": role, "uri": uri, "ts": utcnow()})

    def drain_resource_updates(self, role: str) -> list[str]:
        """Consume this role's event hints; one server per role owns the queue."""
        folder = self.notifications_root / self._notification_role_dir(role)
        if not folder.is_dir():
            return []
        uris = []
        for path in sorted(folder.glob("*.json")):
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                item = {}
            path.unlink(missing_ok=True)
            if item.get("role") == role and isinstance(item.get("uri"), str):
                uris.append(item["uri"])
        return uris

    @property
    def project_root(self) -> Path:
        """The project root, NOT the state dir.

        State lives in `<project>/.pair`, so `self.root` is `.pair`, and matching
        it against a pane cwd is meaningless: a pane of our own project stands
        one level up. Exactly here `pane_is_foreign` broke: `.pair` passed in, and
        "the pane belongs to another project" refused for our own neighbour pane —
        the cross-project injection guard blocked lawful in-project delivery too.

        A separate property, not a silent `parent` at the use site: swapping the
        root in one caller and forgetting in another is exactly how the bug arrived.
        """
        return self.root.parent if self.root.name == ".pair" else self.root

    def default_state(self) -> dict[str, Any]:
        return {
            "task": "",
            "mode": "fix",
            # Default is auto — exactly what the human set: it gives a task, the pair works
            # it alone and returns with one output. `semi` used to stand here, and a fresh
            # project started asking for `ok` on verify phase, which nobody awaited.
            # awaited.
            "autonomy": "auto",
            "writer": None,
            "phase": "scoping",
            "round": 0,
            "created_at": utcnow(),
            "updated_at": utcnow(),
            "roles": {},
            "seen": {},
            "patches": {},
            "evidence": [],
            "human_ok": [],
            "tasks": [],
        }

    def _load(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return self.default_state()
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            log(f"state.json unreadable ({error}); starting from defaults")
            try:
                self.append_transcript("state_corrupt", "server", error=str(error))
            except Exception:
                pass
            return self.default_state()
        state = self.default_state()
        state.update(data)
        # Old states held one task line. Carry it into the list, so the task text never
        # vanishes: past that it appends, never overwrites.
        # Migrate only when no tasks key exists at all — the state truly predates the list.
        # An explicit empty list means "already moved to the new language": raising the
        # task from it again is banned.
        legacy = str(state.get("task") or "").strip()
        if legacy and "tasks" not in data:
            state["tasks"] = [{
                "id": 1, "text": legacy, "status": "open", "added_at": state.get("created_at"),
                "added_by": "human", "round": int(state.get("round", 0)),
                "reason": "", "closed_at": None, "closed_by": None,
            }]
        state.setdefault("tasks", [])
        # Round 106 slice B: the hot file is bounded, so this load must hand back
        # the UNION of the active window and the state archive. A rotated record
        # becomes invisible only to the writer's memory, never to a reader — the
        # same invariant the transcript union carries, and the one a shrunken file
        # would otherwise break.
        try:
            state = _state_rotation_load(self.root, state)
        except Exception as exc:  # noqa: BLE001 - an unreadable archive never hides the active window
            log(f"state archive unreadable ({exc}); serving the active window alone")
        return state

    def _save(self, state: dict[str, Any]) -> None:
        state["updated_at"] = utcnow()
        atomic_write_json(self.state_path, state)

    class _Lock:
        _holders: dict[Path, tuple[int, Any]] = {}

        def __init__(self, path: Path) -> None:
            self.path = path.resolve()

        def __enter__(self):
            entry = Store._Lock._holders.get(self.path)
            if entry is None or entry[0] == 0:
                handle = self.path.open("a+")
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                Store._Lock._holders[self.path] = (1, handle)
            else:
                depth, handle = entry
                Store._Lock._holders[self.path] = (depth + 1, handle)
            return self

        def __exit__(self, *exc):
            entry = Store._Lock._holders.get(self.path)
            assert entry is not None and entry[0] > 0
            depth, handle = entry
            if depth == 1:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()
                del Store._Lock._holders[self.path]
            else:
                Store._Lock._holders[self.path] = (depth - 1, handle)
            return False

    def role_lock_path(self, role: str) -> Path:
        """The per-role singleton file: a live process holds LOCK_EX, the second falls."""
        return self.root / f".server-{role}.lock"

    def acquire_role_lock(self, role: str) -> Any:
        """Take the role slot: a second live process of the same role means refusal, not
        a silent pid collision. Non-blocking LOCK_EX|LOCK_NB: blocking here means
        standing dead, while a refusal shows at once and writes into the journal.
        The handle lives while the process lives: close means release, a dead lock
        never sticks by construction (the kernel releases for the dead).

        Round 111 slice 1: the refusal now DIAGNOSES instead of asserting. "Held
        by a live server" was a claim — a socket daemon outliving its agent by
        design (slice D) and a corpse holding a pid record both read "held", and
        the operator needs to know which of the three they have: kill it, reap a
        record, or start it. Liveness is proved by the lock's own fd-scan, never
        by pgrep (three false DOWN verdicts in one hour, round 108)."""
        path = self.role_lock_path(role)
        handle = path.open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                try:
                    from lock_reaper import describe
                    detail = describe(self.root, role)
                    hint = ("One live role means one live process: kill the double and "
                            "restart. A move is the human will. The daemon now outlives "
                            "its agent by design (slice D), so a held lock may be a "
                            "healthy daemon with no agent beside it — check before killing.")
                except Exception:
                    detail = f"role {role!r} is held by another process"
                    hint = "One live role means one live process."
                raise ProtocolError(f"{detail}; a second process never launches.", hint)
            raise
        return handle

    def lock(self) -> "Store._Lock":
        return Store._Lock(self.lock_path)

    def read(self) -> dict[str, Any]:
        with self.lock():
            return self._load()

    def mutate(self, fn) -> dict[str, Any]:
        """Run fn(state) under the lock and persist the result."""
        with self.lock():
            state = self._load()
            result = fn(state)
            self._save(state)
            return result

    def append_transcript(self, kind: str, role: str, **fields: Any) -> None:
        entry = {"ts": utcnow(), "kind": kind, "role": role}
        entry.update(fields)
        with self.lock():
            entry["prev_hash"] = self._transcript_last_hash()
            line = json.dumps(entry, ensure_ascii=False) + "\n"
            with self.transcript_path.open("a", encoding="utf-8") as handle:
                handle.write(line)
            if self._transcript_over_cap():
                self._rotate_transcript()

    def _transcript_over_cap(self) -> bool:
        try:
            return self.transcript_path.stat().st_size > TRANSCRIPT_MAX_BYTES
        except OSError:
            return False

    def _transcript_last_hash(self) -> str:
        """The chain tail under the same lock as append: two racing appends would read one
        tail and fork the chain. Call only under lock."""
        tail = _transcript_tail(self.transcript_path, 1) if self.transcript_path.exists() else []
        if not tail:
            return CHAIN_GENESIS
        return _transcript_hash(tail[0])

    def _rotate_transcript(self) -> None:
        """Sliding window: the active file never grows without bound.

        The append-only journal — rightly so — but "forever" here meant "while disk
        lasts". Measuring had to come earlier, and the measure came: `pair_transcript`
        by default takes 40 records (~2k tokens), so the agent never drags the whole
        file into its context and never did. So rotation does NOT "remove 46k tokens"
        as the plan stood: those 46k are the file size divided by 4, never reaching
        context.

        What rotation truly fixes, both points measured:
        - tail reading stops parsing the whole file (69 ms and 2 MB on an 11,830-line
          file for 20 reply lines);
        - limitless `pairctl log` stops pouring half a megabyte into the terminal.

        The old never drops silently: it leaves for `transcript-archive/`, where it stays
        reachable. Deleting history silently is banned — a lost `role_collision` record
        cost us a night, and the human must hold it at hand.
        """
        try:
            if self.transcript_path.stat().st_size <= TRANSCRIPT_MAX_BYTES:
                return
        except OSError:
            return
        archive = self.root / "transcript-archive"
        # The chain continues through rotation: the tail last_hash reads BEFORE the shift,
        # while the file still stands. Past os.replace nothing reads — a genesis.
        last = self._transcript_last_hash()
        try:
            archive.mkdir(parents=True, exist_ok=True)
            # The shift: the oldest chunk leaves for `.1`, and so on. Numbers last 99 shifts,
            # and at 99 deleting the oldest beats falling with FileExistsError mid journal-write.
            # FileExistsError mid a journal write.
            for index in range(TRANSCRIPT_ARCHIVES - 1, 0, -1):
                older = archive / f"{index}.jsonl"
                if not older.exists():
                    continue
                if index == TRANSCRIPT_ARCHIVES - 1:
                    older.unlink()
                else:
                    older.rename(archive / f"{index + 1}.jsonl")
            os.replace(self.transcript_path, archive / "1.jsonl")
        except OSError:
            # Rotation must stay harmless: a write failure outranks its order. Falling silent
            # here is banned, but dropping everything else over the archive is needless too —
            # so the trace stays in the journal itself.
            return
        with self.transcript_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "ts": utcnow(), "kind": "transcript_rotated", "role": "system",
                "archive": "transcript-archive/1.jsonl",
                "why": f"the active journal breached {TRANSCRIPT_MAX_BYTES} bytes",
                "prev_hash": last, "last_hash": last,
            }, ensure_ascii=False) + "\n")

    def read_transcript(self, limit: int = 0) -> list[dict[str, Any]]:
        """Journal records; `limit` counts the last ones, 0 means "the whole active".

        The tail reads from the file end, never parsing all in a row. Parsing whole
        for twenty reply lines is no diligence but a time failure on a big journal, and
        our journal grows on its own.
        """
        if not self.transcript_path.exists():
            return []
        if not limit:
            entries: list[dict[str, Any]] = []
            with self.transcript_path.open("r", encoding="utf-8") as handle:
                for raw in handle:
                    entry = _transcript_entry(raw)
                    if entry is not None:
                        entries.append(entry)
            return entries
        return _transcript_tail(self.transcript_path, limit)

    def transcript_entry_count(self) -> int:
        """How many records the active file holds, with no parsing.

        Needed so `pairctl log` can say "shown 50 of 900" instead of reading as a
        full journal. Parsing lines for a count is banned: on a hundreds-of-megabytes
        file the same time failure the tail read was rewritten for.
        """
        try:
            with self.transcript_path.open("rb") as handle:
                return sum(1 for line in handle if line.strip())
        except OSError:
            return 0


def atomic_write_json(path: Path, payload: Any) -> None:
    handle, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _next_entry(lines: list[str]) -> int:
    """The next whole record index in the note-lines list.

    Records start with `## `. Returning len(lines) when no next record exists means
    "nothing left to cut" — the caller must take it, or it carries everything
    into the archive.
    """
    for index, line in enumerate(lines):
        if line.startswith("## "):
            return index
    return len(lines)


def is_pid_alive(pid: Any) -> bool:
    """True if pid is an integer of a currently running process.

    A wrapper over `liveness.pid_alive`: one check, called by both the protocol and
    state reading — two copies would drift on the very first edit.
    """
    return liveness.pid_alive(pid)


def _proc_starttime(pid: Any) -> int | None:
    """`liveness.proc_starttime` wrapper (round 103 S3), same drift rule."""
    return liveness.proc_starttime(pid)


def _state_rotation_load(root: Path, state: dict[str, Any]) -> dict[str, Any]:
    """Store._load's rotation merge, imported lazily (server imports state_rotation,
    never the reverse — a top-level import would close a cycle)."""
    from state_rotation import load_state
    return load_state(root, state)


def tmux_command() -> str:
    """tmux, prefixed with this project's socket when one is named.

    Condition (c) of the per-project-socket slice (round 106 slice C): ONE
    resolver for every tmux call site in the tree. A single tmux server serves
    every project on the box, so plain `tmux` makes another project's panes
    visible and addressable here — measured as a message landing in galatour's
    pane, and as two same-basename projects colliding on session name.

    The socket comes from PAIR_PANE_SOCKET (pairup exports it from the resolved
    project root) and is never read from state.json, so binding needs no state
    file. With no socket named — a legacy pair on the default server — plain
    tmux, so the transition never blinds a running pair.
    """
    exe = shutil.which("tmux") or "tmux"
    socket = (os.environ.get("PAIR_PANE_SOCKET") or "").strip()
    if socket and socket != "default":
        return f"{exe} -L {socket}"
    return exe


def same_pane(left, right) -> bool:
    """`composer.same_pane` with tmux found. The logic lives in composer, next to
    `pane_identity`: pane comparison belongs to pane parsing, not the protocol. The
    wrapper exists because three protocol places call, while composer imports lazily.

    Two references prove identical with NO tmux: identical lines are one and the
    same pane by definition. The wrapper used to answer False on missing tmux, and
    the "never ring our own pane" check silently died on a tmux-less machine —
    exactly where no neighbour lives.
    """
    if not left or not right:
        return False
    if str(left) == str(right):
        return True
    tmux = tmux_command()
    if not tmux:
        return False
    from composer import same_pane as _same
    return _same(tmux, left, right)


def pane_exists(pane: str) -> bool:
    """True if tmux still has this pane.

    A pane reference alone proves nothing across a reboot or a session change: %1
    is a short-lived name that a fresh session reuses for an unrelated window, and
    `<session>:<window>.<pane>` points at whatever now sits at that index. That is
    why clearing a stale pane needs this check and not just a dead pid.

    Both reference formats are accepted. The regex used to allow only `%N`, which
    silently broke the moment addresses were recorded: `pane_exists("mail:0.0")`
    answered False for a live pane, so `roster --prune` would have deleted a role
    that was sitting right there, and `_register` would have cleared its pane.

    False whenever tmux is absent — the honest answer is "cannot vouch for it",
    and the caller is deciding whether to drop a record, so failing closed keeps
    the record rather than guessing.

    The verdict lives in composer.pane_alive: these two answer one question and
    were once two implementations, so a fix to one left the other wrong while
    `pairpilot` called the composer's copy. One implementation, two names kept
    for their existing callers.
    """
    tmux = tmux_command()
    if not tmux:
        return False
    from composer import pane_alive
    return pane_alive(tmux, pane)



class PairSession:
    """One agent's view of the shared state, bound to a role at construction."""

    def __init__(self, role: str, store: Store, pane: str = "") -> None:
        if not role or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", role):
            raise SystemExit(f"invalid --role {role!r}: use letters, digits, dot, dash, underscore")
        self.role = role
        self.store = store
        self._explicit_pane = (pane or "").strip()
        self._last_state_hash: str | None = None

    # ---------------------------------------------------------------- helpers

    def _git_fingerprint(self) -> Any:
        """The HEAD+dirty snapshot plus the delivery floor, for the fingerprint.

        Quenched into `None`: no git means no telling two states apart on this
        fingerprint part — never a reason to leave the agent unanswered at all.
        The delivery floor rides here (not a second fingerprint rule): the
        snapshot is local git already on the path, and `statecache` calls it
        when verdicts stand OR delivery applies — so a push between two
        `pair_state` calls moves the key and the stale blocker never survives
        behind `unchanged:true` (A7).
        """
        try:
            from review import delivery_floor
            from review import snapshot as git_snapshot

            snap = git_snapshot(self.store.project_root)
            status, _ = delivery_floor(self.store.project_root)
            snap["delivery"] = status
            return snap
        except Exception:  # noqa: BLE001 - the snapshot must never drop the cache
            return None

    def _heartbeat(self, state: dict[str, Any], request_meta: Any = None) -> None:
        """Mark liveness and bind the OpenCode host session from request metadata.

        A process may survive a host-session change, so this value is refreshed on
        every OpenCode tool call. Missing or malformed metadata clears the old ID:
        wake delivery must fail closed, never target a previous session.
        """
        roles = state.setdefault("roles", {})
        entry = roles.setdefault(
            self.role, {"first_seen": utcnow(), "reads": 0, "sends": 0})
        entry["last_seen"] = utcnow()
        entry["pid"] = os.getpid()
        entry["pid_start"] = _proc_starttime(os.getpid())
        if self.role == "opencode":
            meta = request_meta if isinstance(request_meta, dict) else {}
            session_id = meta.get("ai.opencode/sessionID")
            if isinstance(session_id, str) and 0 < len(session_id) <= 256 and session_id.strip():
                entry["opencode_session_id"] = session_id
            else:
                entry.pop("opencode_session_id", None)

    def _register(self, state: dict[str, Any]) -> None:
        roles = state.setdefault("roles", {})
        current_pid = os.getpid()
        entry = roles.setdefault(self.role, {"first_seen": utcnow(), "reads": 0, "sends": 0})
        tmux = tmux_command()

        # The human override resolves first, because it decides WHOSE pane this is, while
        # everything else in this function looks at the pane derived from the process
        # environment anyway. The order is no cosmetics: the "pane held by another role"
        # check below must see exactly the pane that records — or the override silently
        # breeds two contenders for one composer.
        # contenders for one composer.
        # The pin check lives in `pairctl_panes` next to the command using it, imported
        # lazily for the same reason as `composer`: the module imports `server`, and at
        # top level that would cycle.
        from pairctl_panes import PIN_OK, PIN_UNKNOWN, pin_verdict
        from composer import pane_belongs_to_project as _belongs
        from composer import pane_identity as _ident
        from composer import pane_key as _pane_key

        pin = entry.get("pane_pin")
        pin_why = ""
        pin_unverified = False
        if pin:
            verdict, pin_why = pin_verdict(tmux, pin, self.store.project_root)
            if verdict == PIN_OK:
                pane = pin
            elif verdict == PIN_UNKNOWN:
                # Nothing to check with never means "dead address". The pin stays in force, but
                # printing into that pane is banned, so `pane` stays empty with the address shown
                # nowhere. The event lands in the transcript, or a silent "never printed" would
                # read as delivered.
                pin_unverified = True
                pane = ""
                self.store.append_transcript("pane_pin_unverified", self.role,
                                             pin=pin, why=pin_why)
            else:
                # A pin with a stale address drops, but never silently: a silently dropped override
                # reads as one that never was, and the human would believe it holds.
                # never was, and the human would believe it holds.
                entry.pop("pane_pin", None)
                entry.pop("pane_pin_why", None)
                self.store.append_transcript("pane_pin_dropped", self.role,
                                             pin=pin, why=pin_why)
                pin = ""

        # Route: pin, then the explicit channel, then env, then ancestry.
        # The explicit channel (--pane or PAIR_PANE) beats inherited TMUX_PANE (say, from
        # a detached service/daemon with a foreign env). Env is NO identity proof but a claimed
        # address: the OpenCode MCP daemon inherits the launching shell pane (measured
        # 2026-10-07: the opencode daemon with omp-pane TMUX_PANE). So the explicit channel
        # and env take only when: (1) the pane lives and is project — belongs_to_project
        # proves; (2) no other LIVE role holds it — or the address is foreign.
        # A refuted route never becomes an address (fail-closed): a "pane_explicit_stale" or
        # "pane_env_stale" event, active delivery mailbox-only.
        # mailbox-only.
        explicit_pane = (self._explicit_pane or os.environ.get("PAIR_PANE") or "").strip()
        env_pane = os.environ.get("TMUX_PANE") or ""
        pane = pin or ""
        proved_explicit = ""
        if explicit_pane and tmux and not pin:
            try:
                ours, _why = _belongs(tmux, explicit_pane, self.store.project_root)
            except Exception:
                ours = False
            occupant_alive = False
            if ours:
                probe_key = _pane_key(tmux, explicit_pane)
                for _rn, _ri in roles.items():
                    if _rn == self.role or not _ri.get("pane"):
                        continue
                    if _pane_key(tmux, _ri["pane"]) != probe_key:
                        continue
                    _pid = _ri.get("pid")
                    if isinstance(_pid, int) and _pid > 0 and _pid != current_pid \
                            and is_pid_alive(_pid):
                        occupant_alive = True
                        break
            if ours and not occupant_alive:
                proved_explicit = explicit_pane
                pane = proved_explicit
            else:
                self.store.append_transcript(
                    "pane_explicit_stale", self.role, pane=explicit_pane, proved=proved_explicit,
                    why="the explicit channel never confirmed as a route (alive, project, no foreign live role)")
        proved_env = ""
        if not pane and env_pane and tmux and not pin:
            try:
                ours, _why = _belongs(tmux, env_pane, self.store.project_root)
            except Exception:
                ours = False
            occupant_alive = False
            if ours:
                probe_key = _pane_key(tmux, env_pane)
                for _rn, _ri in roles.items():
                    if _rn == self.role or not _ri.get("pane"):
                        continue
                    if _pane_key(tmux, _ri["pane"]) != probe_key:
                        continue
                    _pid = _ri.get("pid")
                    if isinstance(_pid, int) and _pid > 0 and _pid != current_pid \
                            and is_pid_alive(_pid):
                        occupant_alive = True
                        break
            if ours and not occupant_alive:
                proved_env = env_pane
                pane = proved_env
            else:
                self.store.append_transcript(
                    "pane_env_stale", self.role, pane=env_pane, proved=proved_env,
                    why="the env pane never confirmed as a route (alive, project, no foreign live role)")
        if not pane and tmux and os.environ.get("PAIR_DISABLE_ANCESTRY_PANE") != "1":
            # A live line is sought over the process tree; the test fixture plants a flag so
            # fill takes no extra address record (287ff1d).
            from composer import find_own_pane
            pane = find_own_pane(tmux) or ""
        # An address refused as foreign. Empty while nothing to refuse: or with `pane == ""`
        # the variable would stay undefined and stale-record cleanup would fall with NameError
        # on every paneless call.
        rejected = ""

        # The pane PROVES before the roster records, and that is no paranoia but a
        # reproduced defect.
        #
        # `TMUX_PANE` arrives from tmux, not the project: an agent project pane launched from another
        # project pane honestly sees its pane — recorded into this project roster as ours.
        # The roster on this machine is shared per role, while projects run several: one
        # project took another pane as neighbour: `pair_deliver` left for another composer
        # (the `pane_is_foreign` guard beat it off), while `pair_state` showed a neighbour
        # with a pane lying in another repo. The human saw "the agent works in another
        # project", literally true.
        # project", literally true.
        #
        # Fail-closed, as in `pane_is_foreign`: unprovable ours means never record.
        # An empty pane beats a wrong one, because a wrong one addresses another composer
        # while an empty one just takes delivery away.
        provisional = ""
        if pane and not pin:
            from composer import pane_belongs_to_project

            ours, why_not_ours = pane_belongs_to_project(
                tmux, pane, self.store.project_root)
            if not ours:
                self.store.append_transcript(
                    "pane_env_rejected", self.role, pane=pane, why=why_not_ours,
                )
                # An unproven pane stores as provisional, never dropped: printing into it is banned
                # (fail-closed holds), but the address lives on, healed by the next proof instead of
                # human hands.
                provisional = pane
                rejected = pane
                pane = ""
        # One live role means one live process (#19.2). A second process under the same role
        # name from another pane is no restart but a double: silently overwriting the pid
        # split the slot between two agents, and delivery struck wide. So refusal
        # (ProtocolError turns into a `refusal` with a transcript record in invoke), not a
        # record: the roster untouched.
        #
        # Refusal only when something checks by — or a false refusal becomes an eternal
        # lockout of a legitimate agent:
        # - a recorded pid lives (os.kill sees another process too, so this fact alone never
        #   suffices — pids recycle);
        # - the comer owns a pane — an addressless call claims no composer;
        # - the held pane is known: the human pin beats the old record, or a human role
        #   move would bounce as a double;
        # - the held pane lives in tmux: a stale address plus a live process means a restart
        #   after a pane death, not a second agent (with no tmux pane_exists always False —
        #   no tmux means no refusal at all, nothing to check by);
        # - panes differ through same_pane: the same pane means the same agent restarting,
        #   the pid updates below as before.
        #   the pid updates below as before.
        # Anything unconverged means only `role_collision` in the transcript, as before:
        # tell, never beat off.
        existing = roles.get(self.role)
        if existing and existing.get("pid") and existing["pid"] != current_pid:
            if is_pid_alive(existing["pid"]):
                self.store.append_transcript(
                    "role_collision",
                    self.role,
                    current_pid=current_pid,
                    existing_pid=existing["pid"],
                )
                held = pin or entry.get("pane")
                if pane and held and pane_exists(held) and not same_pane(pane, held):
                    raise ProtocolError(
                        f"role {self.role!r} is already held by a live process "
                        f"(pid {existing['pid']}, pane {held}); the second process "
                        "never registers on the same role.",
                        "One live role means one live process: leave the double and "
                        "come in under your own role name. A move is the human will: "
                        "`pairctl pane <role> <pane>`, then the new pane turns "
                        "held, and the refusal leaves.",
                    )

        # Another role claims this pane. Clearing its pane on a dead pid alone cost
        # us a night: the panel was alive, its process belonged to a role that had
        # merely restarted, so delivery to a live peer failed with "has no tmux
        # pane" while the peer sat two windows away, waiting.
        #
        # So a pane goes away only on two independent facts: the process is gone
        # AND tmux no longer has the panel. After a reboot or a session change the
        # panel is absent too, which is what makes this correct in the other
        # direction — never clearing would leave a phantom %1 pointing at whatever
        # window now owns that id.
        # Stale-record cleanup walks the address, not the "own pane" fact:
        # when our own pane is refused as foreign, cleanup is still due — or a
        # role with a dead pid and a missing pane would sit in the roster forever.
        # The "is there our own pane" check would silently switch it off.
        for other_name, other_info in list(roles.items()):
            if other_name != self.role and same_pane(pane or rejected,
                                                     other_info.get("pane")):
                other_pid = other_info.get("pid")
                if other_pid and not is_pid_alive(other_pid) \
                        and (not pane_exists(other_info.get("pane"))
                             or (explicit_pane and same_pane(explicit_pane, other_info.get("pane")))):
                    other_info.pop("pane", None)
                    self.store.append_transcript(
                        "pane_stale_cleared", self.role, pane=pane or rejected,
                        other_role=other_name, other_pid=other_pid,
                    )
                elif other_pid:
                    self.store.append_transcript(
                        "pane_collision",
                        self.role,
                        pane=pane or rejected,
                        other_role=other_name,
                        other_pid=other_pid,
                        other_alive=is_pid_alive(other_pid),
                    )

        entry = roles.setdefault(self.role, {"first_seen": utcnow(), "reads": 0, "sends": 0})
        entry["last_seen"] = utcnow()
        entry["pid"] = current_pid
        entry["pid_start"] = _proc_starttime(current_pid)
        if pin and not pin_unverified:
            # The override beats the environment. Exactly this lay broken: hand-editing
            # state.json fixed nothing, because `_register` rewrote `pane` on every call of
            # any tool. Such a "fix" lived exactly till the next ring.
            # "fix" lived exactly till the next ring.
            #
            # The transcript records the difference, not the fact itself: the update runs on
            # every tool, and a record on every call would bury the journal in identical
            # lines — exactly the ceiling fixed here.
            if entry.get("pane") != pin:
                self.store.append_transcript("pane_pin_honoured", self.role,
                                             pane=pin, was=entry.get("pane"),
                                             why="the human override")
            entry["pane"] = pin
            entry["pane_pin_why"] = pin_why
        elif pin_unverified:
            # The pin stays, but the address never stays `pane`. Or an unchecked address
            # would ride into delivery, while the whole fail-closed point is that nothing
            # sends there: the old `pane` record is the same address, just from the last
            # successful confirmation, and keeping it would print into an unchecked pane.
            # and keeping it would print into an unchecked pane.
            # The cause records, so the human sees why the pane is gone.
            entry.pop("pane", None)
            entry["pane_pin_why"] = f"{pin_why}; unconfirmed, the pane stays unused"
        elif pane:
            # Write the stable `<session>:<window>.<pane>` address, not %N: the number
            # renumbers and goes stale in the process environment, the name with a window
            # never does. No backward compatibility to pick: pane_exists, deliver and
            # pane_is_foreign take both shapes, or the upgrade would break delivery for all
            # live roles at once.
            #
            # The transcript never reads "address changed" but "address IS CHANGING": the
            # record repeats on every call of any tool, and on a literal compare it would land
            # in the transcript dozens of times per session, eating exactly the ceiling fixed
            # here.
            address = ""
            if tmux:
                # Exactly session_address, not canonical_pane: the latter returns the raw input on
                # refusal — reads as success — leaving "no address" nothing to notice by.
                # refusal — reads as success — leaving "no address" nothing to notice by.
                from composer import session_address
                address = session_address(tmux, pane)
            previous = entry.get("pane")
            if address and address != previous:
                self.store.append_transcript(
                    "pane_address_canonical", self.role,
                    previous=previous or pane, canonical=address,
                    why="%N renumbers and goes stale; the session stays stable")
            # No address came out — silently staying on %N is banned: then the unstable-
            # addressing cause stays invisible forever. The transcript record is the case where
            # a missing fact is itself a fact.
            # is itself a fact.
            elif not address and previous != pane:
                self.store.append_transcript(
                    "pane_address_unavailable", self.role,
                    pane=pane,
                    why="the session name never resolved, %N addressing stays unstable")
            entry["pane"] = address or pane
            entry.pop("pane_provisional", None)
        elif provisional:
            # An unproven but live pane: held as provisional — delivery printing banned, poll
            # works, the address heals on the next proof. Dropping here returned blindness every
            # tick.
            entry["pane_provisional"] = provisional
            entry.pop("pane", None)
        else:
            # Neither a pin, nor our own pane, nor provisional — while the roster already holds
            # an address. Exactly the infected case: the pane recorded before any check existed,
            # living forever with no explicit drop. Drops with a transcript record, because a
            # quiet fix reads as no pane ever was.
            # a quiet fix reads as no pane ever was.
            # never.
            stale = entry.get("pane")
            # A record added by the owner (pane strained) but dropped here differs from one
            # never checked: the peer proves its own pane in its _register; dropping here only
            # chases blindness.
            # Exactly what broke test 4094: peer.tool_pair_read after sending dropped the %55
            # only the peer itself wrote.
            if stale and not entry.get("pane_provisional"):
                entry.pop("pane", None)
                entry.pop("pane_pin_why", None)
                self.store.append_transcript(
                    "pane_stale_dropped", self.role, pane=stale,
                    why="the pane never proven ours and never confirmed by a pin",
                )

            # The NEIGHBOUR foreign pane drops here too, not only ours. The cause matches:
            # an infected record recorded before the check, and waiting for the role owner to
            # re-register itself is banned — it may never show, while `pair_state` keeps showing
            # the neighbour on another project pane, with `pair_deliver` aiming into it.
            # the neighbour on another project pane, with `pair_deliver` aiming into it.
            # keeps aiming into it.
            #
            # Project membership checks, not liveness: another path drops a dead pane, never
            # mix the two facts.
            if tmux:
                from composer import pane_belongs_to_project

                for other_name, other_info in list(roles.items()):
                    if other_name == self.role:
                        continue
                    other_pane = other_info.get("pane")
                    if not other_pane or other_info.get("pane_pin"):
                        continue
                    theirs, why_theirs = pane_belongs_to_project(
                        tmux, other_pane, self.store.project_root)
                    if not theirs:
                        other_info.pop("pane", None)
                        self.store.append_transcript(
                            "pane_foreign_dropped", self.role, other_role=other_name,
                            pane=other_pane, why=why_theirs,
                        )

        # The roster self-heal (#19.1 + #19.3): neighbour pane recheck and dead-entry expiry
        # on EVERY call, not only with no pane of ours. The old branch above dropped only
        # the foreign project and only with no pane of ours; recheck covers stale, foreign
        # and held panes always, expire deletes paneless entries past the quiet TTL.
        # and held panes always; expire deletes paneless entries past the quiet TTL.
        # Logic in roster_heal (decisions on ready facts), only the call and the journal
        # records here. With no changes — silent, or every call would write into the journal
        # (the same ceiling as pane_pin_honoured).
        if tmux:
            import roster_heal as _heal
            # Reconciled on the pane unique ID: opened means living, closed means dead.
            # The `ml:0.2` address floats on splits, `%N` on tmux restarts; `%2751` is unique
            # and dies with the pane. Arrived with our own UID but recorded another — the pane
            # swapped, the record stays untouched.
        try:
            # The UID reads from the PROVEN pane (pin/ancestry), not env: daemon env runs
            # foreign, and checking another pane is garbage.
            _uid_probe = (pane or env_pane) if tmux else ""
            _here = _ident(tmux, _uid_probe) if _uid_probe else {}
        except Exception:
            _here = {}
        _uid_here = (_here or {}).get("pane_uid") or ""
        if _uid_here:
            for _rn, _ri in list(roles.items()):
                if _rn == self.role or not isinstance(_ri, dict):
                    continue
                _their_uid = str(_ri.get("pane_uid") or "")
                if _their_uid and _their_uid == _uid_here:
                    self.store.append_transcript(
                        "pane_uid_collision", self.role, other_role=_rn,
                        pane_uid=_uid_here,
                        why="two roles on one unique pane — delivery cannot tell apart")
                    break
        if pane and not pin:
            try:
                _cur = _ident(tmux, pane) if tmux else {}
            except Exception:
                _cur = {}
            if _cur and _cur.get("exists") and _cur.get("pane_uid"):
                entry["pane_uid"] = _cur["pane_uid"]
                entry["pane_session_id"] = _cur.get("session_id") or ""
        elif pane and pin and tmux and pane_exists(pane):
            # Pin never refreshed the UID — fixed 2026-10-07: daemon stale env left another
            # pane UID forever. The real pane proves by the same tmux query, with no inherited
            # proved by the same tmux query, with no inherited env.
            try:
                _cur = _ident(tmux, pane)
            except Exception:
                _cur = {}
            if _cur.get("exists") and _cur.get("pane_uid"):
                entry["pane_uid"] = _cur["pane_uid"]
                entry["pane_session_id"] = _cur.get("session_id") or ""
        if tmux:
            for event in _heal.recheck_peers(
                    roles, self.role, self.store.project_root,
                    pid_alive=is_pid_alive, pane_exists=pane_exists,
                    belongs_to_project=lambda pane, root: _belongs(tmux, pane, root),
                    same_pane=same_pane):
                kind = event.pop("kind")
                self.store.append_transcript(kind, event.pop("role"),
                                             **event)
            for event in _heal.expire_dead_roles(
                    roles, state.get("writer"), pid_alive=is_pid_alive):
                kind = event.pop("kind")
                self.store.append_transcript(kind, event.pop("role"),
                                             **event)

    def _require_not_writer(self, action: str) -> None:
        state = self.store.read()
        if state.get("writer") == self.role:
            raise ProtocolError(
                f"{self.role} holds write authority, so it must not {action}.",
                "The writer edits project files directly. Only a non-writer proposes patches.",
            )

    def _require_fix_mode(self, action: str) -> None:
        """Refuse an action that only makes sense in a round that changes code.

        An audit round has no diff by definition, so a sealed patch in one is either
        the wrong round or an agent working from stale instructions. Either way the
        cheap answer is a refusal that names the mode, and an audit record of it.
        """
        state = self.store.read()
        if state.get("mode", "fix") == "audit":
            raise ProtocolError(
                f"this is an audit round (mode=audit), so there is nothing to {action}.",
                "Findings go to pair_note with kind 'finding', confirmed findings become "
                "docs/backlog entries via the writer. Use `bin/pairctl mode fix` to change "
                "the mode back.",
            )

    def _known_roles(self, state: dict[str, Any]) -> list[str]:
        return sorted(state.get("roles", {}))

    def _policy(self, state: dict[str, Any]) -> dict[str, Any]:
        """The standing role policy, set by the human.

        Without it every round starts with a race, and a race is wasted time plus
        a reason to stall mid-work. With it all is known upfront: one always writes,
        the other always reviews, and neither spends a move disputing.

        A human decision, so stored as a human decision, not a pair_claim side effect.
        """
        policy = state.get("policy")
        if isinstance(policy, dict) and policy.get("writer"):
            return policy
        # The global policy holds only when its writer REALLY works in this project.
        # Otherwise it harms: the named role misses here, never appears, and nobody
        # ever takes write authority — the work stalls with a cause nobody phrases.
        # stalls with a cause nobody phrases.
        roles = state.get("roles") or {}
        if roles:
            inherited = read_global_policy()
            if inherited and inherited.get("writer") in roles:
                return inherited
        return {}

    # ------------------------------------------------------------------ tools

    def tool_pair_state(self) -> dict[str, Any]:
        """Read the shared state, the roster, and this role's standing."""
        with self.store.lock():
            state = self.store._load()
            self._register(state)
            self.store._save(state)

        # Cache: unchanged state answers short instead of full (~939 tokens, called 3-4
        # times per round by agents). The fingerprint counts in statecache — knowing
        # neither git nor where obligation files lie, it cannot drift from the answer.
        # files lie, it cannot drift from the answer.
        if not statecache.cache_disabled():
            alive = {
                name: liveness.pid_alive(info.get("pid"))
                for name, info in (state.get("roles") or {}).items()
                if isinstance(info, dict) and info.get("pid")
            }
            state_hash = statecache.fingerprint(
                state,
                obligation_names=[p.name for p in
                                  self.store.obligations_root.glob("*.json")],
                inbox_names=[p.name for p in self._inbox_files()],
                peer_inbox_names=self._peer_inbox_names(state),
                git_snapshot=self._git_fingerprint,
                alive=alive,
                restart_line=restart_trace.restarts_last(self.store.root),
            )
            if state_hash == self._last_state_hash:
                return statecache.unchanged_answer(state_hash)
            self._last_state_hash = state_hash

        writer = state.get("writer")
        policy_writer = self._policy(state).get("writer")
        # `_load` merges whatever was on disk over the defaults, so a hand-edited or
        # newer-server state can carry a mode this build does not know. Reading it as a
        # fix round is the safe direction: an unrecognised value must never be a licence
        # to edit, and must never be able to crash an agent's first pair_state call.
        mode = state.get("mode", "fix")
        if mode not in MODES:
            mode = "fix"
        autonomy = autonomy_of(state)
        current_phase = state.get("phase", "scoping")
        you_are_writer = writer == self.role
        if you_are_writer:
            backward = PHASE_BACKWARD.get(current_phase)
            forward = PHASE_TRANSITIONS.get(current_phase, ())
            legal_next = list(forward)
            if backward and backward not in legal_next:
                legal_next.append(backward)
        else:
            backward = PHASE_BACKWARD.get(current_phase)
            legal_next = [backward] if backward else []

        return {
            "you": self.role,
            "you_are_writer": you_are_writer,
            "task": _subject(state),
            "mode": mode,
            # What the mode means for this role, stated here because it decides what an
            # agent is supposed to do and the standing instructions are written for the
            # default. An audit round changes no project file, so a writer's job in one
            # is evidence, not a diff.
            "mode_meaning": {
                "fix": (
                    "Code gets changed: the writer edits project files, the critic proposes "
                    "sealed patches, git ends in a branch and a PR."
                ),
                "audit": (
                    "No code gets changed on purpose. Do not edit code, do not open "
                    "a branch, do not propose a patch. Findings go to pair_note; "
                    "each one is checked by the peer, confirmed ones become "
                    "docs/backlog entries (a new file there is an allowed result, "
                    "not a code change), and the deliverable is evidence rather "
                    "than a diff."
                ),
            }[mode],
            "writer": writer,
            "autonomy": autonomy,
            "autonomy_meaning": {
                "observe": (
                    "The human is driving. The supervisor speaks but does not act: it will not "
                    "restart a dead agent and will not confirm a permission prompt."
                ),
                "semi": (
                    "The pair works unattended, but closing a round needs recorded evidence and "
                    "one word from the human (`bin/pairctl ok`). §12 stops you either way."
                ),
                "auto": (
                    "Unattended to the end: with evidence recorded, the round may close itself. "
                    "Only §12 stops you."
                ),
            }[autonomy],
            # Standing policy, when the human set it. Then the role is known upfront and no
            # write-authority dispute ever arises.
            "policy": self._policy(state) or None,
            # What I owe whom to hear. Shown to both: or only the supervisor knows about the
            # wait — exactly what nobody knows.
            "obligations": {
                # "Waiting" means obligations I opened. "Awaited from me" means obligations where
                # I receive. Confusing them is dangerous: one leads to waiting, the other to debt,
                # and they cure oppositely.
                "i_am_waiting_for": self._obligations_where("from", self.role),
                "they_wait_for_me": self._obligations_where("to", self.role),
            },
            "my_standing": (
                # Policy names the role, the holder is a fact. They never always match: a dead
                # holder blocks the named one (claim demands an empty writer), and text with no
                # holder reads as permission to write at you_are_writer=false.
                # permission to write at you_are_writer=false.
                # Writer-pin: only the writer pins, the critic is any second live one — its name
                # never stores anywhere and never names in texts.
                "writer by standing policy, the right is mine"
                if policy_writer == self.role and writer == self.role
                else "writer by standing policy, the right is free — take it via pair_claim"
                if policy_writer == self.role and not writer
                else f"assigned writer, the right held by {writer!r} — take it via pair_claim, refuses only the live"
                if policy_writer == self.role
                else f"critic (writes {policy_writer!r}, holds {writer!r})"
                if policy_writer
                else "unassigned, decided by race"
                if not writer
                else f"race for the right, held by {writer!r}"
            ),
            # Why this one writes. Without it the neighbour sees only the fact, never whether
            # to dispute the pick or take it.
            "writer_reason": state.get("writer_reason", ""),
            "phase": current_phase,
            "round": state.get("round", 0),
            "phases": list(PHASES),
            "legal_next_phases": legal_next,
            "roles": self._known_roles(state),
            "unread": self._unread_count(state),
            # [R101] A read batch that was returned but never ACKed. The agent must
            # see it on every state call — not only when it happens to re-read —
            # or "delivered but unprocessed" is indistinguishable from "empty".
            "pending_read": read_delivery.pending_summary(state, self.role),
            "peers": self._peer_status(state, writer),
            # What else blocks the round close. The agent must see it BEFORE spending a move
            # on a doomed pair_advance("done").
            "closing_blocked_by": self._done_blockers(state),
            "evidence_this_round": [
                {"kind": item.get("kind"), "value": item.get("value"), "ok": item.get("ok"),
                 "role": item.get("role")}
                for item in self.round_evidence(state)
            ],
            "awaiting_human_ok": (autonomy == "semi" and not self.human_ok_for_round(state)),
            "unresolved_patches": self.unresolved_patches(state) if mode != "audit" else [],
            # [R102] The last thing pairrestart did, one durable line. A role
            # killed by a restart reads here on reconnect WHO killed it, when,
            # and which pids — instead of learning it from the human (the
            # round-101 channel-dark period).
            "restarts_last": restart_trace.restarts_last(self.store.root),
            "server_fresh": server_freshness.check_server_freshness()["fresh"],
            "server_stale_files": server_freshness.list_stale_files(),
            # [R91] Delivery advisory (B7): the PR ceiling cannot run here — `gh`
            # is network on the hot path — but the writer must not be surprised
            # at close. Local floor status rides along; PR state is verified at
            # advance-to-done, where the message names the exact cure.
            "delivery": self._delivery_advisory(state, autonomy, mode),
            "how_to_transfer_authority": (
                "Only the human can move write authority: `pairctl assign <role>`. "
                "A peer message never transfers it, in either direction."
            ),
        }

    def _delivery_advisory(self, state: dict[str, Any], autonomy: str,
                           mode: str) -> dict[str, str]:
        """Local delivery posture for the state answer, never the network.

        Floor status is local git; the ceiling line states the rule without
        checking it (`gh` runs only at advance). Both keep the writer unsurprised
        without paying Trap 1's network cost on the hot path.
        """
        if mode == "audit":
            return {"floor": "n/a", "ceiling": "audit carries no delivery gate"}
        try:
            from review import delivery_floor

            floor_status, floor_detail = delivery_floor(self.store.project_root)
        except Exception:  # noqa: BLE001 - advisory never drops the answer
            floor_status, floor_detail = "unknown", "delivery check failed to run"
        ceiling = (
            "semi: an open PR is verified at close; auto: a merge is verified at close"
            if autonomy in ("semi", "auto")
            else "observe: delivery floor still applies (do less, not do not count)"
        )
        _ = state
        return {"floor": f"{floor_status}: {floor_detail}", "ceiling": ceiling}

    def _peer_status(self, state: dict[str, Any], writer: str | None) -> dict[str, Any]:
        """Neighbour liveness. The logic lives in `liveness`: both agents read it, checked
        there with no server raised. Unread counts here: only the server sees the
        mailbox (P1 — a stall shows in seconds)."""
        unread = self._peer_mailbox(state)[1]
        return liveness.peer_status(state, role=self.role, writer=writer, unread=unread)

    def _unread_count(self, state: dict[str, Any]) -> int:
        seen = int(state.get("seen", {}).get(self.role, 0))
        return max(0, len(self._inbox_files()) - seen)

    def _peer_mailbox(self, state: dict[str, Any]) -> tuple[dict[str, list[str]], dict[str, int]]:
        """Foreign mailboxes in one pass: names for the cache fingerprint, counters for peers.

        Without names a letter to the neighbour never moved the fingerprint: the caller
        got `unchanged` with no new neighbour unread, and the stall stayed invisible.
        """
        seen = state.get("seen") or {}
        names: dict[str, list[str]] = {}
        unread: dict[str, int] = {}
        for name in (state.get("roles") or {}):
            if name == self.role:
                continue
            try:
                files = [p.name for p in (self.store.mailbox_root / name).glob("*.json")]
            except OSError:
                files = []
            names[name] = files
            try:
                read = int((seen.get(name) or 0))
            except (TypeError, ValueError):
                read = 0
            unread[name] = max(0, len(files) - min(read, len(files)))
        return names, unread

    def _peer_inbox_names(self, state: dict[str, Any]) -> dict[str, list[str]]:
        return self._peer_mailbox(state)[0]


    # ------------------------------------------------------------- evidence

    def round_evidence(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        return round_evidence(state)

    def human_ok_for_round(self, state: dict[str, Any]) -> bool:
        return human_ok_for_round(state)

    def unresolved_patches(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        return unresolved_patches(state)

    def _done_blockers(self, state: dict[str, Any]) -> list[str]:
        blockers = done_blockers(state)
        # The review queue and stale verdicts (the review-queue card).
        # The writer never blocks mid-work; the block sits here, before the result lands.
        # Everything fail-open: with no git and no obligation files the round never holds.
        # holds.
        try:
            from review import changed_between, dirty_files, git_head, is_stale, origin_truth
            # Round 105 slice 2b: the origin-truth comparison, the leg the floor
            # never had. The floor answers for local head only, so a reset behind
            # origin read `delivered` (round 104's false pass). Fail-open on
            # `unknown`: no round_base, no git, no origin/master never holds a
            # close — the branch that would block is `behind`, because origin
            # carries commits THIS state does not, so the round would ship a
            # tree that is not what was reviewed.
            truth, truth_why = origin_truth(self.store.project_root,
                                            str(state.get("round_base") or ""))
            if truth == "behind":
                blockers.append(f"the round is not against current origin: {truth_why}. ")
            elif truth == "ahead":
                blockers.append(f"{truth_why} (delivered means origin carries it) ")
            pending = 0
            # Round 105 slice 4b: notes consistency, the leg the kind-file card
            # needed to be enforceable. A note is two writes that must agree —
            # the entry in notes/<kind>.md and pair_note's transcript row. An
            # entry with no journal row is a hand-edit, and a foreign agent's
            # write into our kind file is exactly that shape hiding. Refuses at
            # close, never repaired here: the writer sees their own edit.
            from pair_notes_consistency import check_notes
            notes_ok, notes_why = check_notes(self.store.root)
            if not notes_ok:
                blockers.append(f"the notes disagree with the journal: {notes_why}. ")
            # Round 106 slice E: the board's own machine leg. Round 105 drifted
            # three times post-commit and once pre-commit — a row claiming
            # `delivered <sha>` for another commit's sha, a prerequisite's sha,
            # or a component without its caller. A human caught each one; the
            # catch is mechanical, so it stops being a human's job. Fail-open
            # ONLY when the board is unreadable or git cannot answer: never
            # block a close on a check that could not run.
            board = self.store.root / "plan.md"
            if board.exists():
                try:
                    sys.path.insert(0, str(self.store.project_root / "bin"))
                    import boardcheck as _boardcheck
                    _ok, _problems = _boardcheck.check(board,
                                                       require_ancestor=False)
                    if not _ok:
                        blockers.append(
                            "the board claims what the tree cannot show: "
                            + "; ".join(_problems[:3]) + ". ")
                except Exception as exc:  # noqa: BLE001 - the leg never drops a close
                    log(f"board check skipped ({exc})")
            if self.store.obligations_root.is_dir():
                for path in self.store.obligations_root.glob("*.json"):
                    try:
                        record = json.loads(path.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        continue
                    if record.get("from") and record.get("to"):
                        pending += 1
            if pending:
                blockers.append(
                    f"a pending review queue never empties (pending_reviews={pending}). "
                    f"The round never closes over an unread verdict: "
                    f"await answers via pair_read or close the extra requests."
                )
            verdicts = [v for v in (state.get("verdicts") or []) if isinstance(v, dict)]
            if verdicts:
                last = verdicts[-1]
                old = last.get("rev") or {}
                current = git_head(self.store.project_root)
                if current:
                    stale, why = is_stale(old, current,
                                          changed_between(self.store.project_root,
                                                          str(old.get("head") or ""), current),
                                          dirty_files(self.store.project_root))
                    if stale:
                        blockers.append(
                            f"the last verdict {last.get('role')}/{last.get('kind')} "
                            f"stale against HEAD: {why}. "
                            f"Applying it to new code is banned — ask for a re-review."
                        )
        except Exception:  # noqa: BLE001 - the review gate never drops the close
            pass
        try:  # an evidence signature never drops the close; with no key — as before
            import rolekeys as _keys2
            for item in round_evidence(state):
                env = item.get("envelope")
                if not isinstance(env, dict):
                    continue
                vk = _keys2.load_verify_key(self.store.root, item.get("role", ""))
                if vk is None:
                    continue
                text = f"{item.get('round')}:{item.get('kind')}:{item.get('task') or ''}:{item.get('value')}"
                if not _keys2.verify_envelope(vk, env, text):
                    self.store.append_transcript("bad_signature", self.role,
                                                 evidence_kind=item.get("kind"),
                                                 evidence_value=str(item.get("value"))[:120])
                    blockers.append(
                        f"evidence {item.get('kind')!r} with a bad signature "
                        f"never counts: {item.get('value')!r}. "
                        f"Rewrite the fact under your own key via pair_evidence."
                    )
        except Exception:  # noqa: BLE001 - the signature never drops the close
            pass
        # [R91] Delivery floor: HEAD visible from a remote-tracking ref.
        # Kills the one-disk result (round 88: 94 commits sat on one filesystem
        # with origin/master at the parent). Local refs only — this method also
        # runs on every pair_state (Trap 1), and `gh`/ls-remote here would turn
        # the hot path into network. Blocks at every autonomy level including
        # observe: observe means "do less, not do not count" (PROTOCOL §10b),
        # and the message names the one push command that cures it. `unknown`
        # (no git, no remote, never fetched) never blocks — a gate that cannot
        # run must never look like one that passed.
        if state.get("mode", "fix") != "audit":
            try:
                from review import delivery_floor

                floor_status, floor_detail = delivery_floor(self.store.project_root)
            except Exception:  # noqa: BLE001 - the floor never drops the close
                floor_status, floor_detail = "unknown", "delivery check failed to run"
            if floor_status == "undelivered":
                blockers.append(
                    f"round {int(state.get('round', 0))} is not delivered: {floor_detail}."
                )
        return blockers

    def _inbox_files(self) -> list[Path]:
        inbox = self.store.mailbox_root / self.role
        if not inbox.is_dir():
            return []
        return sorted(inbox.glob("*.json"), key=lambda p: p.name)

    def _writer_is_dead(self, state: dict[str, Any], writer: str) -> bool:
        """The holder is dead as a process and as a pane: no live pid AND no pane.

        Two facts, not one: the kernel recycles pids, tmux recycles the pane
        (pane_exists fail-closed with no tmux, so with no tmux this is False and the
        record lives — guessing death is banned). A live holder never drops by this
        path: the long _writer_is_abandoned suite guards here.
        """
        info = (state.get("roles") or {}).get(writer) or {}
        pid = info.get("pid")
        if pid is not None and is_pid_alive(pid):
            return False
        pane = info.get("pane")
        if pane and pane_exists(pane):
            return False
        if pid is None and not pane:
            return False
        return True

    def _writer_is_abandoned(self, state: dict[str, Any], writer: str) -> bool:
        """True when the holder closed the round and has gone quiet long enough
        that it no longer holds authority in any useful sense.

        Off by default. Enable with PAIR_STALL_SECONDS=1800 in the environment of
        both MCP servers. Requires a closed round deliberately: a writer pausing to
        run a long suite mid-task must never lose the tree.
        """
        if STALL_SECONDS <= 0:
            return False
        if state.get("phase") != "done":
            return False
        last_seen = (state.get("roles", {}).get(writer) or {}).get("last_seen")
        if not last_seen:
            return False
        try:
            silent = (datetime.now(timezone.utc) - datetime.fromisoformat(last_seen)).total_seconds()
        except Exception:
            return False
        return silent >= STALL_SECONDS

    def tool_pair_claim(self, reason: str = "") -> dict[str, Any]:
        """Take write authority. Refused only while someone else actively holds it.

        `reason` is optional but meant to be used. Whoever reads the task first knows
        more than whoever merely got there first, so saying why is what turns a race
        into a choice. It goes into the transcript and into the refusal message, so
        the loser can see the argument and decide whether to contest it.
        """
        def apply(state: dict[str, Any]) -> dict[str, Any]:
            self._register(state)
            policy = self._policy(state)
            designated = policy.get("writer")
            current = state.get("writer")
            holder_dead = bool(current and current != self.role and self._writer_is_dead(state, current))
            if designated and designated != self.role:
                raise ProtocolError(
                    f"the role-policy writer {designated!r}, and you are the critic.",
                    "This is the human decision, not a race: work as the critic, never spend a move disputing. "
                    "Only a human may change the policy: `pairctl roles --swap`.",
                )
            if designated and designated == self.role and not state.get("writer"):
                state["writer"] = self.role
                state["writer_reason"] = "assigned by role policy"
                self._pin_round_base(state)
                return {
                    "already_writer": False,
                    "writer": self.role,
                    "round": state.get("round", 0),
                    "by_policy": True,
                }
            was_closed = bool(state.get("closed_at") or state.get("phase") == "done")
            if current and current != self.role:
                dead = holder_dead and (not designated or designated == self.role)
                by_policy_takeover = bool(designated and designated == self.role and holder_dead)
                if not self._writer_is_abandoned(state, current) and not was_closed and not dead:
                    held = str(state.get("writer_reason") or "").strip()
                    raise ProtocolError(
                        f"write authority is held by {current!r}"
                        + (f", which claimed it because: {held}" if held else "."),
                        "Ask the human: `pairctl release` or `pairctl assign <role>`. "
                        "If you would rather write, say so in pair_send and let the "
                        "current writer release it with pair_release.",
                    )
                state["writer"] = self.role
                state["round"] = int(state.get("round", 0)) + 1
                self._pin_round_base(state)
                if by_policy_takeover:
                    state["writer_reason"] = "assigned by role policy (holder dead)"
                elif reason.strip():
                    state["writer_reason"] = reason.strip()
                if was_closed:
                    state.pop("closed_at", None)
                    state["phase"] = "implement"
                return {
                    "already_writer": False,
                    "writer": self.role,
                    "round": state["round"],
                    "took_over": True,
                    "by_policy": by_policy_takeover,
                }
            if current == self.role:
                if reason.strip():
                    state["writer_reason"] = reason.strip()
                if was_closed:
                    state.pop("closed_at", None)
                    state["phase"] = "implement"
                    state["round"] = int(state.get("round", 0)) + 1
                    self._pin_round_base(state)
                    return {"already_writer": False, "writer": self.role, "round": state["round"], "took_over": False}
                return {"already_writer": True, "writer": self.role, "round": int(state.get("round", 0)), "took_over": False}
            state["writer"] = self.role
            state["round"] = int(state.get("round", 0)) + 1
            self._pin_round_base(state)
            if reason.strip():
                state["writer_reason"] = reason.strip()
            if was_closed:
                state.pop("closed_at", None)
                state["phase"] = "implement"
            return {"already_writer": False, "writer": self.role, "round": state["round"], "took_over": False}

        result = self.store.mutate(apply)
        self.store.append_transcript("claim", self.role, writer=self.role,
                                     reason=reason.strip() or None, result=result)
        # This note is read by the agent the instant it becomes the writer, which makes it
        # the worst possible place to state a rule that is false in an audit round. pair-guard
        # cannot correct it: it rewrites the standing brief, not a tool result.
        if self.store.read().get("mode", "fix") == "audit":
            note = ("You are the writer, and this is an audit round (mode=audit), so you do NOT "
                    "edit project files, do not branch and do not commit. Run the audit with your "
                    "peer: keep findings in notes, refuse to record one the peer has not checked, "
                    "put confirmed ones in docs/backlog/, and advance the phase.")
        else:
            note = "You are the writer. Edit project files directly. Your peer stays read-only."
        return {
            "writer": result["writer"],
            "already_held": result["already_writer"],
            "round": result.get("round"),
            "took_over": result.get("took_over", False),
            # Never won a race but took the role by human decision: the difference matters,
            # because a role dispute misses the point here.
            "by_policy": result.get("by_policy", False),
            "note": note,
        }

    def tool_pair_release(self, why: str = "") -> dict[str, Any]:
        """Give up write authority voluntarily. Only the holder may do this.

        This is the honest way to hand over: the holder decides the other one is
        better placed. A peer cannot take authority away, and cannot revoke it, but
        the holder can give it — that is a decision, not a transfer by message.
        """
        def apply(state: dict[str, Any]) -> dict[str, Any]:
            current = state.get("writer")
            if current != self.role:
                raise ProtocolError(
                    f"{self.role} does not hold write authority; {current!r} does.",
                    "A peer cannot revoke authority. Ask the human if this needs to change.",
                )
            state["writer"] = None
            state["writer_reason"] = ""
            return {"released": True}

        self.store.mutate(apply)
        self.store.append_transcript("release", self.role, reason=why.strip() or None)
        return {
            "released": True,
            "note": (
                "Authority is now unassigned. Nobody holds write authority. If you meant your peer "
                "to take over, say so in pair_send and let them call pair_claim; the human can also "
                "assign it with `pairctl assign <role>`."
            ),
        }

    def _ring_refused(self, target: str, kind: str, pane: str, reason: str) -> dict[str, Any]:
        """One exit for every ring refusal: a durable row, then the answer.

        Every rung=False leaves the same trace (reason + pane + addressee) —
        the silent-refusal class has nowhere left to hide.
        """
        self.store.append_transcript("doorbell_refused", self.role,
                                     to=target, bell_kind=kind, pane=pane, reason=reason)
        return {"rung": False, "why": reason}

    def _ring(self, target: str, kind: str, detail: str = "") -> dict[str, Any]:
        """Knock on the peer's tmux pane so a sleeping agent wakes itself.

        An idle process cannot be woken any other way: it has to be handed the keys.
        That is the whole reason this exists. The body is already in the mailbox or in
        a note, so what gets typed is a short trigger, safe to push into a composer
        where a whole paragraph would mangle.

        `detail` is NOT a place for the message text. The human directive: the
        terminal is a display, not a bus between agents (task #9). `pair_deliver`
        now reads "data in the mailbox, a pointer in TUI", while this path stayed second
        and carried 50 chars of the letter into the neighbour composer — exactly what
        the directive banned. Invisible because the suite pins `PAIR_DISABLE_RING=1`:
        no test ever touched this code. What lies in the mailbox the neighbour learns
        from `pair_read`; only the cause name leaves from here.
        """
        if os.environ.get("PAIR_DISABLE_RING") == "1":
            return {"rung": False, "why": "ring disabled by PAIR_DISABLE_RING"}
        state = self.store.read()
        pane = (state.get("roles", {}).get(target) or {}).get("pane")
        if not pane:
            return self._ring_refused(target, kind, "", "no pane, the neighbour is outside tmux")
        # String comparison never serves here: our own pane arrives from the environment
        # as `%1` while state holds `mail:0.1`. Such equality just stops holding, and the
        # ring leaves for our own pane.
        if same_pane(pane, os.environ.get("TMUX_PANE")):
            return self._ring_refused(target, kind, pane, "never ring our own pane")
        tmux = tmux_command()
        if not tmux:
            return self._ring_refused(target, kind, pane, "tmux not found in PATH")
        try:
            probe = subprocess.run(
                [tmux, "display-message", "-p", "-t", pane, "#{pane_dead}"],
                capture_output=True, text=True, timeout=5,
            )
        except OSError as exc:
            # tmux died between which() and launch, the pane is gone — the ring is not
            # guaranteed to deliver, but no fix for this round either (the §10 lesson: fixing
            # the master never outranks delivering the letter).
            return self._ring_refused(target, kind, pane, f"the neighbour pane never verifies: {exc}")
        if probe.returncode != 0 or probe.stdout.strip() == "1":
            return self._ring_refused(target, kind, pane, "the neighbour pane is dead")
        # Liveness is not identity. The pane number outlives the death of the process that
        # held it, and several pair-mailbox projects run on this machine: ringing the
        # recorded number woke another agent.
        from composer import pane_is_foreign
        foreign, why_foreign = pane_is_foreign(tmux, pane, self.store.project_root)
        if foreign:
            # An unproven pane bans typed printing (send-keys) — that is injection into
            # another composer. Fallback is display-message (a popup with no printing) plus
            # mailbox. No silence, no injection.
            try:
                subprocess.run([tmux, "display-message", "-t", pane,
                                "-d", "4000",
                                f"[pair] {kind} from {self.role} — take it from the mailbox"],
                               capture_output=True, text=True, timeout=5)
            except Exception as exc:  # noqa: BLE001 - the pane vanished between checks
                return self._ring_refused(target, kind, pane, f"pane unreadable: {exc}")
            self.store.append_transcript("doorbell_fallback", self.role, to=target,
                                         bell_kind=kind, pane=pane, reason=why_foreign)
            return {"rung": True, "pane": pane, "fallback": "display-message"}
        from composer import capture, composer_line, ring_refusal_reason
        # Single-shot with no waiting: the ring is an optimization, not a condition.
        # The wake/*.json file (written by the caller on the event) is the guarantee the
        # pairpilot polls; waiting for the composer to free here means hanging the tool
        # for 1.5s and grabbing the neighbour input.
        # Busy/invisible means straight back to the file channel.
        try:
            shot = capture(tmux, pane)
            line = composer_line(shot)
        except Exception as exc:  # noqa: BLE001 - the pane vanished between checks
            return self._ring_refused(target, kind, pane, f"pane unreadable: {exc}")
        reason = ring_refusal_reason(shot, line)
        if reason is not None:
            return self._ring_refused(target, kind, pane, reason)
        prompt = f"pair: {kind} from {self.role}. Read pair_read."
        if detail:
            # A cause with no letter text. Trimmed text here would be a "bus in parts": past
            # one line it never fits anyway, reading as a stub no cause follows from.
            # a stub no cause follows from.
            prompt += f" ({detail})"
        first = subprocess.run([tmux, "send-keys", "-t", pane, "-l", prompt],
                               capture_output=True, text=True, timeout=5)
        second = subprocess.run(tmux_command().split() + ["send-keys", "-t", pane, "Enter"],
                                capture_output=True, text=True, timeout=5)
        if first.returncode != 0 or second.returncode != 0:
            reason = (f"send-keys text rc={first.returncode} enter rc={second.returncode}"
                      + (f" {second.stderr.strip()[:120]}" if second.stderr else ""))
            self.store.append_transcript("doorbell_enter_unverified", self.role,
                                         to=target, bell_kind=kind, pane=pane, reason=reason)
            return {"rung": False, "why": reason, "pane": pane}
        # Round 97 C5: rc==0 is not submission — the composer may retain the
        # prompt (seen live post-96-merge). Verify via capture-pane, retry
        # Enter once, then record unverified instead of claiming woken.
        def _retained() -> bool:
            try:
                line = composer_line(capture(tmux, pane))
            except Exception:  # noqa: BLE001 - pane vanished mid-ring
                return True
            return line is not None and prompt in line
        if _retained():
            retry = subprocess.run([tmux, "send-keys", "-t", pane, "Enter"],
                                   capture_output=True, text=True, timeout=5)
            if _retained():
                reason = ("composer retained prompt after Enter "
                          f"(rc=0 both sends, retry rc={retry.returncode})")
                self.store.append_transcript("doorbell_enter_unverified", self.role,
                                             to=target, bell_kind=kind, pane=pane, reason=reason)
                return {"rung": False, "why": reason, "pane": pane}
        self.store.append_transcript("doorbell", self.role, to=target, bell_kind=kind, pane=pane)
        return {"rung": True, "pane": pane, "prompt": prompt}

    def tool_pair_unblock(self, peer: str = "") -> dict[str, Any]:
        """Check whether a peer's tmux pane is stuck on a permission prompt, and
        confirm it only if the prompt is on the allowlist.

        Use when the peer agent has stalled or silent_seconds is growing, to prevent
        sessions from blocking on CLI prompts.

        The allowlist is why this is not a free "y". Anything that writes, runs or
        deletes is refused by default and reported, because an agent left alone with
        a destructive prompt is better than one that says yes on your behalf.
        """
        from composer import _tmux, resolve_permission_prompt

        target = peer.strip()
        state = self.store.read()
        roles = state.get("roles", {})
        if not target:
            candidates = [r for r in roles if r != self.role]
            if len(candidates) == 1:
                target = candidates[0]
            elif not candidates:
                return {"unblocked": False, "why": "no registered neighbours", "prompt": ""}
            else:
                raise ProtocolError("`peer` is required when multiple peers are registered.")
        if target == self.role:
            raise ProtocolError("cannot unblock own role.")
        pane = (roles.get(target) or {}).get("pane")
        if not pane:
            return {"unblocked": False, "why": f"with {target} has no tmux pane", "prompt": ""}
        try:
            tmux = _tmux()
        except Exception as exc:
            return {"unblocked": False, "why": str(exc), "prompt": ""}
        allow, on_default = load_allowlist(self.store.root)
        # `allow or None` used to turn the default list into DEFAULT, while empty meant
        # "confirm nothing". Now the list always reads sane: empty means an explicit human
        # ban, not a missing file.
        try:
            result = resolve_permission_prompt(tmux, pane, allow=allow)
            if on_default and result.get("approved"):
                self.store.append_transcript(
                    "unblock_default_allowlist", self.role, target=target, pane=pane,
                    prompt=result.get("prompt"), matched=result.get("matched"),
                    note="permissions.json missing, confirmed on the default list")
        except Exception as exc:  # noqa: BLE001 - another pane must never drop the server
            return {"unblocked": False, "why": f"pane unreadable: {exc}", "prompt": ""}
        # P2 (round 75): the journal records not the bare regex-pattern name but the
        # parsed command — the whole exact_command and argv by segments. The verdict already
        # passed with keys pressed in resolve; the log pins what was agreed to: a swap past
        # logging shows at the journal-vs-fact reconcile, never drowns in silence.
        # logging shows at the journal-vs-fact reconcile, never drowns in silence.
        try:
            from composer import split_command_segments as _split_segments
            _segments = _split_segments(result.get("prompt") or "")
        except Exception:  # noqa: BLE001 - the log must never block the unblock
            _segments = []
        exact_command = "; ".join(_segments) if _segments else (result.get("prompt") or "")
        matched = result.get("prompt") or ""
        if result.get("approved"):
            self.store.append_transcript("unblock", self.role, target=target, pane=pane,
                                         prompt=matched, matched=result.get("matched"),
                                         exact_command=exact_command, argv=_segments)
        elif result.get("verdict") == "refuse":
            self.store.append_transcript("unblock_refused", self.role, target=target, pane=pane,
                                         prompt=matched, why=result.get("why"),
                                         exact_command=exact_command, argv=_segments)
        return {
            "unblocked": bool(result.get("approved")),
            "peer": target,
            "pane": pane,
            "prompt": matched,
            "verdict": result.get("verdict"),
            "why": result.get("why"),
            "status": (
                f"Confirmation [y] sent: {matched}"
                if result.get("approved")
                else f"Permission request NOT confirmed ({result.get('why')}): {matched}"
                if result.get("verdict") == "refuse"
                else "No permission request found in the pane"
            ),
        }

    def tool_pair_deliver(self, to: str, text: str, wait_for_empty: float = 0.0, submit: bool = True) -> dict[str, Any]:
        """Hand the message to the neighbour: data in the mailbox, a pointer in TUI only.

        The human directive: the terminal is a display for the human, not a bus
        between agents. The old version printed the FULL neighbour-composer text and
        called itself "active path". Wrong beyond aesthetics: on a torn reconcile
        Enter never pressed, the text stayed in another window, the message duplicated
        into the mailbox — the human saw the same thing twice. Observed live twice.

        The only honest order:
        1. the text lands in `.pair/mailbox` — a container depending neither on tmux
           nor anyone attention;
        2. the composer prints a short POINTER the neighbour itself takes the
           message or digest from.

        The pointer rides second, because a pointer with no message is a lie: the
        neighbour obeys, visits the mailbox and finds nothing.

        The decision price to name: the neighbour may skip the mailbox on a short
        pointer, while a sheet in its composer would read itself. A deliberate trade
        of text delivery for on-demand delivery.
        """
        from composer import ComposerBusy, deliver

        if not isinstance(to, str) or not to.strip():
            raise ProtocolError("`to` is required.")
        to = to.strip()
        if to == self.role:
            raise ProtocolError("cannot deliver a message to your own role.")
        if not isinstance(text, str) or not text.strip():
            raise ProtocolError("`text` is required.")

        # 1. Data goes in the container. Always, regardless of tmux and pane.
        sent = self.tool_pair_send(to=to, text=text, subject="")
        seq = sent.get("seq")

        state = self.store.read()
        pane = (state.get("roles", {}).get(to) or {}).get("pane")
        result: dict[str, Any] = {"delivered": True, "seq": seq,
                                  "in_pane": False, "chars": len(text)}
        if not pane:
            result["why"] = f"with {to} has no tmux pane — message in the mailbox"
            return result

        from composer import _tmux, pane_is_foreign
        try:
            foreign, why_foreign = pane_is_foreign(_tmux(), pane, self.store.project_root)
        except ComposerBusy as exc:
            foreign, why_foreign = True, f"tmux unreachable: {exc}"
        if foreign:
            # An unproven pane bans typed printing (injection into another composer).
            # Fallback is display-message + mailbox only.
            import subprocess as _sp
            try:
                _sp.run([_tmux(), "display-message", "-t", pane,
                         "-d", "4000",
                        # Round 103 S2: the pointer carries its mail seq so a
                        # reader can tell this pointer's vintage — one stale
                        # pointer text was indistinguishable from three fresh ones.
                         f"[pair] {self.role}: new message in the mailbox (seq {seq}), read pair_read"],
                        capture_output=True, text=True, timeout=5)
            except Exception as exc:  # noqa: BLE001 - the pane vanished between checks
                result["why"] = f"pane unreadable: {exc}"
                return result
            self.store.append_transcript(
                "deliver_pointer_fallback_foreign_pane", self.role, to=to,
                pane=pane, reason=why_foreign, seq=seq,
            )
            result["in_pane"] = True
            result["fallback"] = "display-message"
            result["delivered_to_pane"] = pane
            return result

        # 2. The pointer. Short on purpose: a line in another TUI, not text.
        # Round 103 S2: carries the mail seq — a reader must never need the
        # filesystem to know this pointer's vintage.
        pointer = f"[pair] {self.role}: new message in the mailbox (seq {seq}), read pair_read"
        try:
            pointed = deliver(pane, pointer, wait_for_empty=wait_for_empty, submit=submit)
        except ComposerBusy as exc:
            self.store.append_transcript("deliver_pointer_busy", self.role, to=to,
                                         reason=str(exc), seq=seq)
            result["why"] = f"composer busy ({exc}); message in the mailbox"
            return result
        self.store.append_transcript("deliver", self.role, to=to, seq=seq, **pointed)
        result["in_pane"] = bool(pointed.get("delivered"))
        result["delivered_to_pane"] = pane
        if pointed.get("unverified"):
            # The pointer may stay in the composer — ONE line, not a paragraph, and the
            # neighbour scrubs it itself. Nothing to duplicate into the mailbox: it already
            # carries the text alone.
            result["why"] = "pointer unconfirmed; message in the mailbox"
        return result

    def _obligations_where(self, side: str, role: str | None) -> list[dict[str, Any]]:
        # A thin call: the logic lives in obligations.py (server-py-still-a-monolith).
        return obligations.list_obligations(self.store, side, role)

    def _open_obligation(self, frm: str, to: str, seq: int, subject: str = "") -> dict[str, Any]:
        return obligations.open_obligation(self.store, frm, to, seq, subject)

    def _answer_obligations(self, answerer: str, author: str) -> list[int]:
        return obligations.answer_obligations(self.store, answerer, author)

    def tool_pair_send(self, to: str, text: str, subject: str = "",
                        expects_reply: bool = False) -> dict[str, Any]:
        """Send one message to the peer. The label identifying you is added here,        not by you, so a peer cannot forge authorship.

        Set `expects_reply` when the message asks for a decision, a review or an action. That
        writes an obligation the supervisor watches, so a request nobody answered cannot end the
        night in silence.
        """
        if not isinstance(to, str) or not to.strip():
            raise ProtocolError("`to` is required.")
        to = to.strip()
        if not isinstance(text, str) or not text.strip():
            raise ProtocolError("`text` is required.")
        if to == self.role:
            raise ProtocolError("cannot send a message to your own role.")
        if expects_reply:
            # The review-queue cap: at most 2 unacked (the card). Five open reviews meant
            # answers ever later and ever rarer, missing their revision. On the third — stop
            # and read.
            try:
                from review import MAX_OPEN_REVIEWS, queue_full
                if queue_full(self.store.obligations_root, self.role):
                    raise ProtocolError(
                        f"review queue full ({MAX_OPEN_REVIEWS} open). "
                        f"Never add requests silently: enter "
                        f"pair_read(wait_seconds=30) and await the verdicts, "
                        f"or answers land past your revision."
                    )
            except ProtocolError:
                raise
            except Exception:  # noqa: BLE001 - counting never drops the send
                pass

        def apply(state: dict[str, Any]) -> dict[str, Any]:
            self._register(state)
            roles = state.setdefault("roles", {})
            roles[self.role]["sends"] = int(roles[self.role].get("sends", 0)) + 1
            if to not in roles:
                # A role off-roster is no "never started yet" but a typo or a phantom (round-47
                # lessons: opencode-agent got a dead mailbox, while the pilot knocked 20 times on
                # a nobody-answering one). Silently creating a mailbox means silently losing the
                # create a mailbox means silently losing the message. A refusal with
                # message. A refusal with the live-role list is the only honest behaviour.
                raise ProtocolError(
                    f"role {to!r} never registered in the roster. "
                    f"Live roles: "
                    f"{', '.join(sorted(n for n, i in roles.items() if i.get('pid'))) or 'nobody'}. "
                    f"Check the role name. When the role must exist — let the "
                    f"agent first call any pair_* tool, it registers it."
                )
            inbox = self.store.mailbox_root / to
            # The dir may never create when the letter is the role first.
            inbox.mkdir(parents=True, exist_ok=True)
            existing_seqs = [0]
            if self.store.mailbox_root.is_dir():
                for f in self.store.mailbox_root.glob("*/*.json"):
                    try:
                        existing_seqs.append(int(f.name.split("-")[0]))
                    except Exception:
                        pass
            current_max_seq = max(existing_seqs)
            seq = max(state.get("seq", 0), current_max_seq) + 1
            state["seq"] = seq
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            token = uuid.uuid4().hex[:6]
            filename = f"{seq:06d}-{stamp}-{self.role}-{token}.json"
            body = {
                "seq": seq,
                "from": self.role,
                "to": to,
                "ts": utcnow(),
                "subject": subject or "",
                "label": f"Chat from {self.role}: ",
                "text": text.strip(),
            }
            try:  # with no key the letter leaves unsigned, the signature never drops the send
                import rolekeys as _keys
                sk = _keys.load_signing_key(self.store.root, self.role)
                if sk is not None:
                    body["envelope"] = _keys.sign_envelope(
                        sk, _keys.key_id_for(self.store.project_root, self.role),
                        seq, self.role, to, body["ts"], body["text"])
            except Exception:  # noqa: BLE001
                pass
            atomic_write_json(inbox / filename, body)
            return {"seq": seq, "known_target": to in roles}

        closed = self._answer_obligations(to, self.role)
        result = self.store.mutate(apply)
        self.store.append_transcript("send", self.role, to=to, seq=result["seq"],
                                     subject=subject or text.strip()[:60])
        obligation = None
        if expects_reply:
            obligation = self._open_obligation(self.role, to, seq=result["seq"],
                                               subject=subject or text.strip()[:60])
        # Event wake-up is a file, not printing (wake-without-composer). The letter already
        # sits in mailbox, the file guarantees the pairpilot poll; the _ring below is only
        # the fast-response optimization.
        try:
            from wake import record_wake
            # Round 103 S2: the wake text carries the seq.
            record_wake(self.store.root, self.store.read(), to, "message",
                        f"[pair] {self.role}: new message in the mailbox (seq {result['seq']}), read pair_read")
            self.store.append_transcript("wake_pending", self.role, to=to,
                                         reason="message", seq=result["seq"])
        except Exception:  # noqa: BLE001 - the wake file must never drop the send
            pass
        try:
            # A file hint for the recipient subscriptions: the live MCP server of that role
            # picks it up on a watcher thread and notifies the client. With no subscriptions
            # the files bother nobody and zero out.
            self.store.publish_resource_update(to, f"mailbox://{to}")
        except Exception:  # noqa: BLE001 - push must never drop the send
            pass
        ring = self._ring(to, "message")
        return {
            "delivered": True,
            "woke_peer": ring["rung"],
            "wake": ring.get("prompt") or ring.get("why"),
            "seq": result["seq"],
            "awaits_answer": expects_reply,
            "obligation": obligation,
            "target_is_registered": result["known_target"],
            "label_applied": f"Chat from {self.role}: ",
            "note": (
                "Delivered. Do not wait for a reply and do not describe the message as though an "
                "answer were owed: the peer may never answer."
            ),
        }

    def tool_pair_read(self, limit: int = 10, wait_seconds: float = DEFAULT_READ_WAIT_SECONDS,
                       ack_token: str | None = None) -> dict[str, Any]:
        """Return mail at least once; a batch stays unread until its token is ACKed."""
        if ack_token is not None:
            try:
                receipt = self.store.mutate(lambda state: read_delivery.acknowledge_batch(
                    state, self.role, ack_token))
            except ValueError as exc:
                raise ProtocolError(str(exc)) from None
            self.store.append_transcript("pair_read_acknowledged", self.role,
                                         batch_id=receipt["batch_id"], seqs=receipt["seqs"])

        try:
            wait = float(wait_seconds)
        except (TypeError, ValueError):
            raise ProtocolError("`wait_seconds` must be a number.") from None
        if wait < 0:
            raise ProtocolError("`wait_seconds` must not be negative.")
        timeout_ms = client_timeout_ms(self.role)
        wait, was_capped = effective_read_wait(wait, timeout_ms)
        deadline = time.monotonic() + wait
        messages: list[dict[str, Any]] = []
        batch: dict[str, Any] | None = None
        while True:
            try:
                messages, batch = self._drain(limit)
            except read_delivery.DrainRefused as exc:
                raise ProtocolError(str(exc)) from None
            if batch or messages or time.monotonic() >= deadline:
                break
            time.sleep(0.1)

        answer: dict[str, Any] = {
            "messages": messages,
            "count": len(messages),
            "waited_seconds": round(wait, 2),
            "note": (
                "Read messages by seq. If `ack_token` is returned, pass it on the next "
                "pair_read after processing the batch. A retry without it returns the "
                "same batch; no other tool call acknowledges mail."
            ),
        }
        if batch:
            answer["pending_read"] = batch
            answer["ack_token"] = batch["batch_id"]
        if was_capped:
            answer["wait_capped_by_client_timeout"] = True
            answer["client_timeout_ms"] = timeout_ms
            answer["max_read_wait_seconds"] = MAX_READ_WAIT_SECONDS
            answer["why"] = (
                f"You asked for more than this client can wait for. It cut the wait to "
                f"{wait:.0f}s (MCP timeout {timeout_ms}ms minus the client margin). Raise "
                f"the role's MCP timeout or ask for less and call again."
            )
        return answer

    def _drain(self, limit: int) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        return read_delivery.drain(self, limit)

    def _pin_round_base(self, state: dict[str, Any]) -> None:
        """Stamp the round's own base: the head origin/master carries right now.

        Round 104's delivery floor answered `delivered` from refs/remotes/* while
        origin sat 7 ahead — there was no recorded base to diff against, so "ahead
        of what?" had no answer. Pinned at round OPEN (every path that increments
        the round), it gives the origin-truth comparison (round 105 slice 2b) a
        number instead of a guess.

        Best-effort by design: a state dir with no git, or no origin/master yet,
        leaves the field unset rather than blocking the round — the comparison then
        reports its own inability, it does not invent a base.
        """
        if state.get("round_base"):
            return
        try:
            from review import unanchored_master_commits, _run_git
            root = self.store.project_root
            head = _run_git(root, "rev-parse", "--verify", "origin/master")
        except Exception:  # noqa: BLE001 - a missing base never blocks an open
            head = None
        if head:
            state["round_base"] = head.strip()
        return None

    def tool_pair_advance(self, phase: str) -> dict[str, Any]:
        """Move the shared phase.

        Forward belongs to the writer. One step back belongs to anyone: a critic
        that found something after the writer closed the round must be able to
        reopen it, and reopening is not a write operation.

        Closing is refused over unread mail, because a report the writer never
        read is not a report.

        In `semi` and `auto` it is also refused without evidence: a round that no
        machine ever checked is a round that finished by opinion. Confident models
        report 100% certainty on most of their own estimates, so "everyone seemed
        happy" is not a signal the gate can accept. §12 stops the pair in every
        autonomy level and is not something this gate can wave through.
        """
        if phase not in PHASES:
            raise ProtocolError(f"unknown phase {phase!r}; expected one of {', '.join(PHASES)}.")

        current = self.store.read()
        from_phase = current.get("phase", "scoping")
        is_writer = current.get("writer") == self.role
        backward = PHASE_BACKWARD.get(from_phase)

        if not is_writer and phase != backward:
            hint = f'You may reopen the round: pair_advance("{backward}")' if backward else ""
            raise ProtocolError(
                f"only the writer moves the phase forward; {current.get('writer')!r} holds authority.",
                hint,
            )

        forward = PHASE_TRANSITIONS.get(from_phase, ())
        if phase not in forward and phase != backward:
            allowed = list(forward)
            if backward and backward not in allowed:
                allowed.append(backward)
            raise ProtocolError(
                f"cannot move from {from_phase!r} to {phase!r}; allowed: {', '.join(allowed) or 'none'}.",
                "Phases move scoping → plan → implement → verify → done. Any role may take one step back.",
            )

        reopening = from_phase == "done" and phase != "done"
        server_freshness.assert_server_fresh(reopening=reopening, backward=(phase == backward))
        gated = from_phase == "plan" and phase == "implement" and not reopening
        intent_gated = from_phase == "scoping" and phase == "plan" and not reopening

        def apply(state: dict[str, Any]) -> None:
            if intent_gated:
                from intent_gate import gate as intent_check
                i_verdict = intent_check(self.store.root, state)
                if not i_verdict.get("ok"):
                    raise ProtocolError(f"the scoping → plan move is banned: {i_verdict['why']}",
                                        "Create .pair/intent.md: `round: N`, Problem, Scope, Outcome.")
            if gated:
                # The plan machine gate: the §9a agreement turns into a check. Not an evidence-gate
                # (§5a) replacement and no touching steps back: a round reopened by a finding past
                # a round reopened by a finding past done never owes a plan — or the gate
                # done never owes a plan — or the gate would lock the reopening on itself.
                from plan_gate import gate as plan_gate_check
                verdict = plan_gate_check(self.store.root, state)
                if not verdict.get("ok"):
                    raise ProtocolError(
                        f"the plan → implement move is banned: {verdict['why']}",
                        "The .pair/plan.md file: first line `round: N`, Goal sections, "
                        "Invariants, Acceptance. The neighbour visa is a kind=verdict note "
                        "with a `plan-ack round N` line (own note never counts), "
                        "or the wait window from .pair/plan-gate.json lapsing with no "
                    )
            if phase != "done" and phase != backward:
                from budget import check_budget
                b_ok, b_why, _ = check_budget(self.store.root, state, phase)
                if not b_ok:
                    raise ProtocolError(b_why, "Reset: `bin/pairctl budget --reset` or closing in done.")
            if phase == "done":
                unread = self._unread_count(state)
                if unread > 0:
                    raise ProtocolError(
                        f"{unread} unread message(s) in your inbox; a round cannot be closed over unread mail.",
                        "Run pair_read first. If the peer still needs something, it has not been said yet.",
                    )
                # [R91] PR ceiling, advance-path only: `gh` is network and must
                # never run in the pair_state hot path (Trap 1). Semi needs an
                # open PR (prepared, human word still owed); auto needs a merge
                # (end to end). Verified against `gh`, never trusted from a
                # kind=pr row (E1). Audit exempt (B6, D3).
                blockers = []
                if state.get("mode", "fix") != "audit":
                    try:
                        from review import pr_ceiling

                        ceil_status, ceil_detail = pr_ceiling(
                            self.store.project_root, autonomy_of(state))
                    except Exception:  # noqa: BLE001 - the ceiling never drops close
                        ceil_status, ceil_detail = "unknown", "PR check failed to run"
                    if ceil_status == "missing":
                        blockers.append(f"round is not shipped: {ceil_detail}.")
                blockers.extend(self._done_blockers(state))
                if blockers:
                    raise ProtocolError(
                        "this round cannot be closed yet: " + " ".join(blockers),
                        "Everything here is checkable. Record the proof, or say in a note which "
                        "part of the task is knowingly left open and why.",
                    )
            state["phase"] = phase
            if reopening:
                state.pop("closed_at", None)
                state["reopened_by"] = self.role
                state["round"] = int(state.get("round", 0)) + 1
                self._pin_round_base(state)
            elif phase == "done":
                import traceback
                state["closed_at"] = utcnow()
                from receipt import GroundTruthBlocked, generate_receipt
                from review import git_head
                try:
                    _head = git_head(self.store.project_root)
                    generate_receipt(self.store.root, state, head_commit=_head)
                except GroundTruthBlocked as exc:
                    raise ProtocolError(f"round cannot be closed: {exc}", "Remove combat mocks, commit and retry.")
                except Exception:
                    traceback.print_exc()
                # Round 94 C9: server-derived manifest beside the receipt.
                from manifest import ManifestRefused, write_manifest
                try:
                    write_manifest(self.store.root, state)
                except ManifestRefused as exc:
                    raise ProtocolError(f"round cannot be closed: {exc}",
                                        "The journal disagrees with the manifest.")
                except Exception:
                    traceback.print_exc()

        self.store.mutate(apply)
        self.store.append_transcript(
            "advance", self.role, phase=phase, previous=from_phase, reopened=reopening
        )
        return {
            "phase": phase,
            "previous": from_phase,
            "reopened": reopening,
            "round": self.store.read().get("round", 0),
        }

    def tool_pair_task(self, action: str, id: int | None = None, text: str = "",
                        reason: str = "") -> dict[str, Any]:
        """The shared task list: one truth for two, not a copy each.

        The list never replaces notes but closes one hole: a task added mid-round
        silently vanished, because state held one task line and the new overwrote
        the old. Proven: the round closed as "task B" with evidence gathered for task A.

        Rights run asymmetric, and that is a decision, not a gap: the critic opens a
        task — or it could never say "I found this too" without writing into another
        note — but the writer closes it, because it implements it.
        """
        action = str(action or "").strip()
        if action not in ("add", "done", "drop", "list"):
            raise ProtocolError(f"unknown task action {action!r}. Use add, done, drop or list.")
        state = self.store.read()

        if action == "list":
            items = tasks_of(state)
            return {
                "tasks": items,
                "open": [item.get("id") for item in items if item.get("status") == OPEN_TASK],
                "next_id": next_task_id(state),
            }

        if action == "add":
            body = str(text or "").strip()
            if not body:
                raise ProtocolError("`text` is required: what has to be done.")
            if len(body) > 2000:
                raise ProtocolError("A task longer than 2000 characters is not a task.")

            def add(st):
                item = {
                    "id": next_task_id(st), "text": body, "status": OPEN_TASK,
                    "added_at": utcnow(), "added_by": self.role,
                    "round": int(st.get("round", 0)), "reason": "",
                    "closed_at": None, "closed_by": None,
                }
                st.setdefault("tasks", []).append(item)
                return item

            item = self.store.mutate(add)
            write_task_banner(self.store.root, self.store.read())
            self.store.append_transcript("task_added", self.role, task_id=item["id"],
                                         text=body[:200])
            return {"task": item,
                    "why_it_matters": "An open task keeps the round from closing until it is "
                                      "done or dropped with a reason."}

        # done / drop — writer only.
        if state.get("writer") != self.role:
            raise ProtocolError(
                f"only the writer closes tasks; {self.role!r} may add and list them.",
                "You found it — add it with pair_task(action=\"add\", text=...) and the writer "
                "will implement it. Closing someone's task is the writer's call.",
            )
        target = int(id or 0)

        def close(st):
            items = st.setdefault("tasks", [])
            match = next((item for item in items if int(item.get("id", 0)) == target), None)
            if not match:
                known = ", ".join(str(item.get("id")) for item in items) or "none yet"
                raise ProtocolError(f"no task #{target}. Known ids: {known}.")
            if match.get("status") != OPEN_TASK:
                raise ProtocolError(
                    f"task #{target} is already {match.get('status')!r}.",
                    "Nothing to change. If it came back up, add a new task that says so.",
                )
            if action == "drop" and not str(reason or "").strip():
                raise ProtocolError(
                    f"task #{target} cannot be dropped without a reason.",
                    "Say in the reason why it is not needed, so the record shows the decision "
                    "instead of a silent gap. If it is done, use action=\"done\".",
                )
            match["status"] = "done" if action == "done" else "dropped"
            match["reason"] = str(reason or "").strip()
            match["closed_at"] = utcnow()
            match["closed_by"] = self.role
            # Round 94 D2: bind by close-round, not add-round.
            match["closed_round"] = int(st.get("round", 0))
            return match

        item = self.store.mutate(close)
        write_task_banner(self.store.root, self.store.read())
        self.store.append_transcript("task_closed", self.role, task_id=item["id"],
                                     status=item["status"], reason=item["reason"][:200])
        remaining = [i["id"] for i in open_tasks(self.store.read())]
        return {"task": item, "still_open": remaining,
                "how_to_use": "Closing is honest bookkeeping. If the work is not actually "
                              "finished, leave it open or drop it with a reason — the round "
                              "cannot close over an open task, and that is the point."}

    def tool_pair_evidence(self, kind: str, value: str, ok: bool = True,
                           note: str = "", task: int | None = None) -> dict[str, Any]:
        """Record a fact a machine produced: tests, lint, a typecheck, a commit, a PR.

        This is what makes a round a result instead of an opinion. `value` is the
        machine's own output — "117 passed in 6.2s", "exit 0", a commit sha, a PR
        URL. `ok=false` is not a confession of failure, it is the record that stops
        the round from closing until somebody deals with it.

        Evidence belongs to the round it was gathered in. Closing round 7 on what
        proved round 3 is the thing this exists to prevent.
        """
        kind = (kind or "").strip()
        if kind not in EVIDENCE_KINDS:
            raise ProtocolError(
                f"`kind` must be one of {', '.join(EVIDENCE_KINDS)}.",
                "Use 'manual' for something a human checked by hand, and say in `note` what "
                "they looked at. An invented kind cannot be checked by anyone.",
            )
        if not str(value or "").strip():
            raise ProtocolError("`value` is required: the machine's own output, not a verdict.")

        def apply(state: dict[str, Any]) -> dict[str, Any]:
            # Which task the fact belongs to. Default — the single open one: usually one
            # runs, and asking every time would force the agent to repeat what the list implies.
            # forcing the agent to repeat what the list implies.
            still_open = [item["id"] for item in open_tasks(state)]
            owner = int(task) if task is not None else (still_open[0] if len(still_open) == 1 else None)
            record = {
                "round": int(state.get("round", 0)),
                "role": self.role,
                "kind": kind,
                "value": str(value).strip(),
                "ok": bool(ok),
                "note": str(note or "").strip(),
                "task": owner,
                # A snapshot of open tasks at record time. With several, the fact binds to none —
                # lying is unwelcome, but losing it is banned either: the journal tells by this list
                # what it belonged to.
                "tasks_open": still_open,
                "at": utcnow(),
            }
            entries = state.setdefault("evidence", [])
            try:  # with no key the fact leaves unsigned, the signature never drops the record
                import rolekeys as _keys
                sk = _keys.load_signing_key(self.store.root, self.role)
                if sk is not None:
                    text = f"{record['round']}:{record['kind']}:{record['task'] or ''}:{record['value']}"
                    record["envelope"] = _keys.sign_envelope(
                        sk, _keys.key_id_for(self.store.project_root, self.role),
                        record["round"], self.role, "", record["at"], text)
            except Exception:  # noqa: BLE001
                pass
            entries.append(record)
            # Current-round records never evict. The close gate reads them from state, not the
            # transcript, so an evicted failure record simply stops counting: a round with red
            # tests would close because the author described in words that all is well. A band
            # for past rounds stays, or state grows without bound.
            # or state grows without bound.
            current = int(state.get("round", 0))
            older = [item for item in entries if int(item.get("round", 0)) != current]
            mine = [item for item in entries if int(item.get("round", 0)) == current]
            del older[: max(0, len(older) - EVIDENCE_KEEP)]
            state["evidence"] = older + mine
            return record

        record = self.store.mutate(apply)
        self.store.append_transcript("evidence", self.role, evidence_kind=kind,
                                     value=record["value"][:200], ok=record["ok"],
                                     round=record["round"])
        if not record["ok"] and self.store.read().get("phase") == "done":
            try:
                from heal import trigger_heal
                trigger_heal(self.store, self.role, record)
            except Exception:
                pass
        state = self.store.read()
        failed = [item for item in self.round_evidence(state) if not item.get("ok")]
        passed = [item for item in self.round_evidence(state) if item.get("ok")]
        return {
            "recorded": record,
            "round": record["round"],
            "passed_this_round": len(passed),
            "failing_this_round": len(failed),
            "closing_blocked_by": ([f"{item.get('kind')}: {item.get('value')}" for item in failed]
                                   or []),
        }

    # ------------------------------------------------------------ patch flow

    def tool_pair_patch_reserve(self) -> dict[str, Any]:
        """Reserve an empty patch file. Refused for the writer, which edits directly."""
        self._require_fix_mode("propose a patch")
        self._require_not_writer("propose patches")
        handle, name = tempfile.mkstemp(
            dir=str(self.store.patches_root), prefix=f"pair-{self.role}-", suffix=".patch"
        )
        os.close(handle)
        path = Path(name)
        os.chmod(path, 0o600)

        def apply(state: dict[str, Any]) -> None:
            state.setdefault("patches", {})[str(path)] = {
                "owner": self.role,
                "created_at": utcnow(),
                "sealed": False,
                "sha256": None,
                "applied": False,
            }

        self.store.mutate(apply)
        self.store.append_transcript("patch_reserve", self.role, path=str(path))
        return {
            "path": str(path),
            "mode": "0600",
            "how_to_use": [
                "Write the unified diff into this exact path with your normal file tools.",
                "Then call pair_patch_seal with this path to compute and record its SHA-256.",
                "Send the peer the path and the hash. Do not apply it yourself.",
            ],
        }

    def _owned_patch(self, state: dict[str, Any], path: str) -> dict[str, Any]:
        """Patch record, for actions only the proposing role may perform."""
        record = state.get("patches", {}).get(path)
        if not record:
            raise ProtocolError(f"{path} was not reserved through pair_patch_reserve.")
        if record.get("owner") != self.role:
            raise ProtocolError(f"{path} belongs to {record.get('owner')!r}, not {self.role!r}.")
        return record

    def _any_patch(self, state: dict[str, Any], path: str) -> dict[str, Any]:
        """Patch record for the writer, who verifies and applies patches proposed
        by the peer and therefore never owns them."""
        record = state.get("patches", {}).get(path)
        if not record:
            raise ProtocolError(f"{path} was not reserved through pair_patch_reserve.")
        return record

    def _releasable_patch(self, state: dict[str, Any], path: str) -> tuple[dict[str, Any], bool]:
        """Record the outcome of applying a sealed patch. Writer only, after a matched verify.

        Own — always. Another — only the writer and only while the patch stays unresolved.

        A foreign patch used to drop for nobody: the author may, but gone means nobody.
        `pair_patch_applied` never helps — it demands `verified_match` while the file
        may be gone. The round closed impossibly on that, and I stepped in live: the
        neighbour patch hung while the server refused my `pair_patch_release`. The
        transcript record stays mandatory — "dropped by writer" and "dropped by author"
        differ, and the report must tell them apart.
        """
        record = state.get("patches", {}).get(path)
        if not record:
            raise ProtocolError(f"{path} was not reserved through pair_patch_reserve.")
        if record.get("owner") == self.role:
            return record, False
        if state.get("writer") != self.role:
            raise ProtocolError(
                f"{path} belongs to {record.get('owner')!r}, not {self.role!r}.",
                "Only the writer may drop a patch they did not propose.",
            )
        if record.get("applied"):
            raise ProtocolError(
                f"{path} is already recorded as applied by {record.get('applied_by')!r}.",
                "An applied patch is not unresolved. If the apply was wrong, say so in a note "
                "and re-apply by hand; dropping the record here would hide both.",
            )
        return record, True

    def tool_pair_patch_seal(self, path: str) -> dict[str, Any]:
        """Compute and record the hash of a patch this role reserved."""
        self._require_fix_mode("seal a patch")
        if any(char in path for char in GLOB_CHARS):
            raise ProtocolError(f"{path} contains a glob character; exact paths only.")

        def apply(state: dict[str, Any]) -> dict[str, Any]:
            record = self._owned_patch(state, path)
            target = Path(path)
            if not target.is_file():
                raise ProtocolError(f"{path} does not exist.")
            digest = sha256_of(target)
            record["sealed"] = True
            record["sha256"] = digest
            record["sealed_at"] = utcnow()
            record["size"] = target.stat().st_size
            return record

        record = self.store.mutate(apply)
        self.store.append_transcript("patch_seal", self.role, path=path, sha256=record["sha256"])
        return {
            "path": path,
            "sha256": record["sha256"],
            "size": record.get("size"),
            "next": (
                f"Send the peer exactly: {path} sha256 {record['sha256']}. "
                "They must re-verify it immediately before applying."
            ),
        }

    def tool_pair_patch_verify(self, path: str) -> dict[str, Any]:
        """Re-check a sealed patch against its recorded hash. The writer calls this
        immediately before applying; anything other than a match means stop."""
        self._require_fix_mode("verify a patch")
        if any(char in path for char in GLOB_CHARS):
            raise ProtocolError(f"{path} contains a glob character; exact paths only.")
        current = self.store.read()
        if current.get("writer") != self.role:
            raise ProtocolError(
                "only the writer verifies and applies patches.",
                "You proposed it; the writer owns applying it.",
            )

        def apply(state: dict[str, Any]) -> dict[str, Any]:
            record = self._any_patch(state, path)
            if not record.get("sealed"):
                raise ProtocolError(f"{path} was never sealed; there is no hash to check against.")
            target = Path(path)
            if not target.is_file():
                raise ProtocolError(f"{path} has disappeared since it was sealed.")
            actual = sha256_of(target)
            record["verified_at"] = utcnow()
            record["verified_match"] = actual == record.get("sha256")
            record["sha256_at_verify"] = actual
            return record

        record = self.store.mutate(apply)
        match = bool(record.get("verified_match"))
        self.store.append_transcript("patch_verify", self.role, path=path, match=match)
        if not match:
            log(f"HASH MISMATCH on {path}: recorded {record.get('sha256')}, found {record.get('sha256_at_verify')}")
        return {
            "path": path,
            "match": match,
            "recorded_sha256": record.get("sha256"),
            "actual_sha256": record.get("sha256_at_verify"),
            "verdict": (
                "Hash matches. Copy the patch once, apply from the copy, then re-verify before applying."
                if match
                else "STOP. The patch changed after it was sealed. Do not apply it; ask the peer to re-seal."
            ),
        }

    def tool_pair_propose(self, files: list[str], subject: str = "") -> dict[str, Any]:
        """One command instead of six reserve→seal→send→verify→applied steps.

        The server builds the unified diff over the worktree itself (`git diff HEAD --
        files`), reserves and seals the patch itself, drops the writer letter with
        path+hash via `pair_send` with `expects_reply`. The critic only names files —
        the 0600-and-sha ceremony vanishes by hand. Non-writers only: the writer edits
        the tree directly. The manual path (reserve→seal) stays for exotica, marked
        legacy in the protocol.
        """
        self._require_fix_mode("propose a patch")
        self._require_not_writer("propose patches")
        if not isinstance(files, list) or not files:
            raise ProtocolError("`files` is required: non-empty list of paths.")
        clean = [str(item).strip() for item in files if str(item or "").strip()]
        if not clean:
            raise ProtocolError("`files` is required: non-empty list of paths.")
        if any(char in item for item in clean for char in GLOB_CHARS):
            raise ProtocolError("exact paths only, no globs.")
        if any(item.startswith("/") or ".." in item.split("/") for item in clean):
            raise ProtocolError("paths stay inside the project: no absolute, no `..`.")
        proc = subprocess.run(
            ["git", "diff", "--no-ext-diff", "HEAD", "--", *clean],
            cwd=str(self.store.project_root), capture_output=True, text=True,
            timeout=30)
        if proc.returncode != 0:
            raise ProtocolError(f"git diff failed: {(proc.stderr or '').strip()[:200]}")
        diff = proc.stdout
        if not diff.strip():
            raise ProtocolError("no changes in these files: nothing to propose.")
        reserved = self.tool_pair_patch_reserve()
        path = reserved["path"]
        Path(path).write_text(diff, encoding="utf-8")
        sealed = self.tool_pair_patch_seal(path)
        writer = self.store.read().get("writer")
        sent = self.tool_pair_send(
            to=str(writer or ""), subject=subject or f"propose: {', '.join(clean)}",
            text=f"Patch {path} sha256 {sealed['sha256']}: diff over {', '.join(clean)}. "
                 f"Check pair_patch_verify and apply.",
            expects_reply=True)
        return {"path": path, "sha256": sealed["sha256"],
                "size": sealed.get("size"), "files": clean,
                "seq": sent.get("seq"), "to": sent.get("known_target")}


    def tool_pair_patch_applied(self, path: str, ok: bool = True, note: str = "") -> dict[str, Any]:
        """Record the outcome of applying a sealed patch. Writer only.

        The protocol had no verb for this: a patch was sealed and verified, and
        nothing ever said whether it reached the tree. A round could therefore
        close over a proposal that had been sitting untouched since yesterday, and
        nothing in the state, the transcript or the report would admit it. Closing
        a round now requires that every sealed patch is accounted for.

        `ok=false` means the apply failed — a rejected hunk, a conflict, a context
        that no longer matched. That counts as unresolved, so the round stays open
        until it is fixed or the patch is released with a reason.
        """
        if any(char in path for char in GLOB_CHARS):
            raise ProtocolError(f"{path} contains a glob character; exact paths only.")
        current = self.store.read()
        if current.get("writer") != self.role:
            raise ProtocolError(
                "only the writer applies patches.",
                "You proposed it; the writer owns applying it and recording the outcome.",
            )

        def apply(state: dict[str, Any]) -> dict[str, Any]:
            record = self._any_patch(state, path)
            if not record.get("sealed"):
                raise ProtocolError(f"{path} was never sealed, so there is nothing to have applied.")
            if not record.get("verified_match"):
                raise ProtocolError(
                    f"{path} was never verified against its sealed hash.",
                    "Call pair_patch_verify first. Applying a patch nobody re-hashed is how a "
                    "changed proposal gets into the tree under the wrong hash.",
                )
            record["applied"] = bool(ok)
            record["applied_at"] = utcnow()
            record["applied_by"] = self.role
            if note:
                record["applied_note"] = str(note)[:400]
            return record

        record = self.store.mutate(apply)
        self.store.append_transcript("patch_applied", self.role, path=path, ok=bool(ok),
                                     sha256=record.get("sha256"))
        remaining = self.unresolved_patches(self.store.read())
        return {
            "path": path,
            "applied": bool(ok),
            "applied_by": self.role,
            "sha256": record.get("sha256"),
            "still_unresolved": [item["path"] for item in remaining],
            "how_to_use": (
                "Recorded. If the apply failed, fix it and record the passing outcome, or "
                "pair_patch_release with the reason so the round is not held open by a patch "
                "both sides already abandoned."
            ) if not ok else "Recorded. This patch no longer blocks closing the round.",
        }

    def tool_pair_patch_release(self, path: str) -> dict[str, Any]:
        """Delete a patch file and forget its record, by exact path.

        Own patch: always. Someone else's: the writer may, while it is still
        unresolved, and the transcript says the writer did it. A file already gone
        from disk is not a problem — the record is what blocks the round, and this
        is the only thing that removes it.
        """
        if any(char in path for char in GLOB_CHARS):
            raise ProtocolError(
                f"{path} contains a glob character.",
                "Release patches by exact path. Never clean a directory with a glob: two agents "
                "share it and one of them may hold a file you cannot see.",
            )

        def apply(state: dict[str, Any]) -> bool:
            record, by_writer = self._releasable_patch(state, path)
            # The record leaves state (state describes the present, the transcript
            # describes history), but the drop itself is written to the audit trail:
            # "a sealed patch was thrown away" is a fact the report must be able to
            # show, and once the record is gone this is the only place it survives.
            self.store.append_transcript(
                "patch_dropped", self.role, path=path,
                owner=record.get("owner"), sha256=record.get("sha256"),
                sealed=bool(record.get("sealed")), applied=bool(record.get("applied")),
                released_by_writer=by_writer)
            del state["patches"][path]
            return bool(record)

        existed = self.store.mutate(apply)
        deleted = False
        target = Path(path)
        if target.exists():
            try:
                target.unlink()
                deleted = True
            except OSError as error:
                if error.errno != errno.ENOENT:
                    raise
        self.store.append_transcript("patch_release", self.role, path=path, existed=existed, deleted=deleted)
        return {"path": path, "deleted": deleted, "known_to_server": existed}

    @staticmethod
    def _note_body_lines(lines: list[str]) -> list[str]:
        """Lines past the header, cut on the record edge.

        A note holds `## <time> — <role>` blocks. Cutting by lines is banned: half a
        record reads worse than no record, because the reader takes it for whole. So
        the tail starts at the first whole record.
        """
        start = 0
        for index, line in enumerate(lines):
            if line.startswith("## "):
                start = index
                break
        return lines[start:] if start else lines

    def _rotate_note(self, kind: str, path: Path) -> None:
        """Carry old records into archive, leaving the tail and a pointer in the file.

        The count computes at once, NEVER picked iteratively. The iterative form was
        written first and hung forever: past the record-edge cut the next record always
        landed at index 0, so the counter never grew and the loop never ended. Counting
        back from the file length is trivial and cannot loop.
        """
        lines = path.read_text(encoding="utf-8").splitlines()
        if len(lines) <= NOTE_LINE_CAP:
            return
        body = self._note_body_lines(lines)
        if not any(line.startswith("## ") for line in body):
            return
        body_start = lines.index(body[0])
        head = lines[:body_start]
        # The first position where the tail fits the ceiling, rounded forward to the
        # record edge, never tearing a record in half.
        target = max(body_start, len(lines) - NOTE_LINE_CAP)
        while target < len(lines) and not lines[target].startswith("## "):
            target += 1
        archived = lines[body_start:target]
        if not archived:
            return
        stamp = utcnow().replace(":", "").replace("-", "")
        archive_dir = self.store.notes_root / "archive"
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive = archive_dir / f"{kind}-{stamp}.md"
        archive.write_text("\n".join(archived) + "\n", encoding="utf-8")
        pointer = [
            f"**{len(archived)} records carried into "
            f"[archive/{archive.name}](archive/{archive.name}).** The file grows only "
            f"into the tail, deliberately: reading it whole stops making sense, "
            f"read the tail via `pair_note_read`.",
            "",
        ]
        path.write_text("\n".join(pointer + lines[target:]) + "\n", encoding="utf-8")
        self.store.append_transcript(
            "note_rotated", self.role, note_kind=kind,
            archived_lines=len(archived), kept_lines=len(lines) - target)

    def tool_pair_ping(self) -> dict[str, Any]:
        """A cheap sign of life: only `last_seen` (+`pid`), a short answer.

        Every `pair_*` call already moves `last_seen` (heartbeat in `invoke`), but a
        full `pair_state` answer costs ~939 tokens. An agent writing code for 20
        minutes with no calls reads dead to the pilot, alive to the cause — hence
        false ALARMs mid deep work. A ping is the same heartbeat at a three-field
        price: CLIs send it themselves every N minutes, the pilot tells "silent but
        pinging" from "silent and not pinging" apart.
        """
        state = self.store.read()
        entry = (state.get("roles") or {}).get(self.role) or {}
        return {"role": self.role, "last_seen": entry.get("last_seen"),
                "pid": entry.get("pid")}

    def tool_pair_note_index(self) -> dict[str, Any]:
        """The note list with size and freshness — seeing the heap without reading it.

        Notes pile to dozens, the biggest near 19 thousand tokens. This list decides
        what to read, spending zero content tokens on it.
        """
        root = self.store.notes_root
        out: list[dict[str, Any]] = []
        if root.is_dir():
            for path in sorted(root.glob("*.md")):
                try:
                    text = path.read_text(encoding="utf-8")
                except OSError:
                    continue
                out.append({
                    "kind": path.stem,
                    "lines": text.count("\n") + 1,
                    "entries": text.count("\n## "),
                    "bytes": len(text.encode("utf-8")),
                    "archived": (root / "archive" / f"{path.stem}-").exists()
                    or any((root / "archive").glob(f"{path.stem}-*.md"))
                    if (root / "archive").is_dir() else False,
                })
        out.sort(key=lambda item: item["bytes"], reverse=True)
        return {"count": len(out), "total_bytes": sum(i["bytes"] for i in out),
                "notes": out}

    def tool_pair_note_read(self, kind: str, tail_lines: int = NOTE_READ_DEFAULT, limit: int | None = None) -> dict[str, Any]:
        """Read the note TAIL, never the whole.

        A note grows tail-only, and reading it whole is needless almost always: the
        latest matters, not all history ever stored. `tail_lines` caps above, the answer
        always cuts on the record edge, stating outright whether it cut — or the reader
        takes the tail for the whole text. `limit` aliases `tail_lines` (the
        pair_read/transcript habit).
        """
        if limit is not None: tail_lines = limit
        kind = (kind or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", kind):
            raise ProtocolError("`kind` must be a short slug: letters, digits, dot, dash, underscore.")
        if not isinstance(tail_lines, int) or tail_lines < 1:
            raise ProtocolError("`tail_lines` must be a positive integer.")
        tail_lines = min(tail_lines, NOTE_READ_MAX)
        path = self.store.notes_root / f"{kind}.md"
        if not path.is_file():
            raise ProtocolError(f"no note `{kind}`")
        lines = path.read_text(encoding="utf-8").splitlines()
        body = self._note_body_lines(lines)
        # Count BEFORE cutting: or total_lines would equal returned_lines, and the cut
        # mark would turn useless.
        available = len(body)
        truncated = available > tail_lines
        if truncated:
            cut = len(body) - tail_lines
            while cut < len(body) and not body[cut].startswith("## "):
                cut += 1
            body = body[cut:]
            if not any(line.startswith("## ") for line in body):
                # A tail too small for even one record: returning empty text is banned — the caller
                # never tells "empty note" from "tail too small". The last whole RECORD serves — the
                # freshest. Exactly the record, not the last line: the last line is the record body,
                # the very half that must never show.
                # body — exactly the half that must never show.
                everything = self._note_body_lines(lines)
                starts = [i for i, line in enumerate(everything) if line.startswith("## ")]
                if not starts:
                    body = []
                else:
                    body = everything[starts[-1]:]
                cut = available - len(body)
        # total_lines sizes the READABLE tail, not the file: or with no cut returned_lines
        # would mismatch total_lines over the service blank line, with "cut" reading false.
        # would mismatch total_lines over the service blank line, with "cut" reading false.
        # The on-disk file size is its own field, to keep it visible too.
        return {"kind": kind, "total_lines": available, "returned_lines": len(body),
                "truncated": truncated, "tail_lines": tail_lines,
                "file_lines": len(lines),
                "text": "\n".join(body)}

    def tool_pair_note(self, kind: str, text: str, head: str | None = None) -> dict[str, Any]:
        """Append a durable note: a plan, a decision, a finding, a verdict.
        Notes are the shared memory two agents leave behind. Anything one of them
        needs to survive a context reset belongs here, not in the conversation.
        """
        kind = (kind or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", kind):
            raise ProtocolError("`kind` must be a short slug: letters, digits, dot, dash, underscore.")
        if not (text or "").strip():
            raise ProtocolError("`text` is required.")
        self.store.notes_root.mkdir(parents=True, exist_ok=True)
        body = text.strip()
        normalized = "\n".join(line.rstrip() for line in body.splitlines()).strip()
        import hashlib as _hashlib
        body_sha = _hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
        # Round 103 S6, peer amendment 3: attribution first. Five identical
        # blocks landed in handoff.md at one ts and the transcript could not
        # say whether one call appended five times or five calls ran inside a
        # second — the fields that would tell (tool, call id, attempt) were
        # never written. A fact nobody can attribute is a fact nobody trusts.
        attempt: dict[str, int] = {}
        for row in self.store.read_transcript(limit=50):
            if row.get("kind") != "note" or row.get("note_kind") != kind \
                    or row.get("role") != self.role:
                continue
            if str(row.get("note_sha", "")) == body_sha:
                attempt["n"] = int(row.get("note_attempt", 0)) + 1
        n_attempt = attempt.get("n", 1)
        target = self.store.notes_root / f"{kind}.md"
        with self.store.lock():
            with target.open("a", encoding="utf-8") as stream:
                stream.write(f"\n## {utcnow()} — {self.role} (note {body_sha} attempt {n_attempt})\n\n{body}\n")
            # Rotation at 10 KB: notes grow unbounded, and a tail read from a 72 KB file costs
            # ~18000 tokens. Rotation keeps the active part compact, the old leaving for archive/.
            if target.stat().st_size > 10 * 1024:
                self._rotate_note(kind, target)
        # Round 103 S6: normalization closes the one-newline gap (peer
        # amendment 2: blocks 1-4 sha-equal len 1204, block 5 len 1203 — exact
        # equality would refuse four and miss the fifth). The row carries the
        # normalized sha + attempt; repeated content is ATTRIBUTED, not
        # silently swallowed.
        self.store.append_transcript("note", self.role, note_kind=kind,
                                     chars=len(text), note_sha=body_sha,
                                     note_attempt=n_attempt)
        if kind == "verdict" or kind.startswith("verdict-"):
            # The verdict-to-revision bind lives in review next to snapshot. Silently applying a
            # verdict to a drifted revision beats missing verdict. Fail-open: with no git an empty
            # snapshot writes.
            try:
                from review import pin_verdict
                pin_verdict(self.store, self.role, kind, text, utcnow, head=head)
            except ValueError:
                raise
            except Exception:  # noqa: BLE001 - the snapshot never drops the verdict
                pass
            # A verdict with no obligation is a dead end: the critic passed, the writer awaits,
            # with nothing to watch. An obligation on the writer — "take the verdict and close
            # the round" — closes the loop with no human: the pilot nudges the recipient via
            # reply_grace, the author via reply_hard. Measured 2026-10-06: verdict seq 252 lay
            # 3.5 hours, the writer stood on a "waiting verdict" recap, the pilot silent — nothing
            # to watch, no obligation file.
            # to watch, no obligation file.
            try:
                writer = (self.store.read().get("writer") or "").strip()
                if writer and writer != self.role and _verdict_owes_review(body):
                    self._open_obligation(
                        self.role, writer, int(self.store.read().get("seq", 0)),
                        subject=f"verdict {kind} — take it and close the round")
            except Exception:  # noqa: BLE001 - the obligation never drops the verdict
                pass
        # Notes stay quiet: with no fan-out ring to all roles (the pair-note-rings-everyone
        # card, done). The wake file below is the only signal; the pilot polls it via wake_grace.
        # card, done). The wake file below is the only signal; the pilot polls it via wake_grace.
        # Who awaits an answer — pair_send (the point ring) or an obligation on the verdict
        # above. The woke_peers key stays empty for answer compatibility.
        # compatibility.
        try:
            from wake import record_wake
            snapshot = self.store.read()
            for other in snapshot.get("roles", {}):
                if other == self.role:
                    continue
                try:
                    record_wake(self.store.root, snapshot, other, f"note/{kind}",
                                f"[pair] {self.role}: new note {kind}, read pair_note_read")
                    self.store.append_transcript("wake_pending", self.role, to=other,
                                                 reason=f"note/{kind}")
                except Exception:  # noqa: BLE001 - the wake file never drops the note
                    pass
        except Exception:  # noqa: BLE001 - the wake import never drops the note
            pass
        return {"kind": kind, "path": str(target), "woke_peers": {}}
    def invoke(self, name: str, arguments: dict[str, Any], request_meta: Any = None) -> dict[str, Any]:
        """Run one tool by name, recording refusals on the audit trail.

        Every refusal lands in the transcript regardless of transport, so the
        trail answers "what did each agent try, and what was it told?" and not
        just "what succeeded". A peer probing for a way around the rules is
        exactly the event worth keeping.
        """
        handler = HANDLERS.get(name)
        # The sign of life is the call itself, not a picked tool: every ring moves
        # `last_seen`, or an agent writing notes in a closed round reads abandoned
        # (the last-seen-stale card, done). Only the timestamp: panels and pin belong to
        # `_register` alone. Heartbeat runs before the tool, not after: a refusal is also
        # a sign of life, or an agent handed a `refusal` would read dead while just talking
        # to the server. An unknown tool misses this: a name typo is no sign of life, and
        # growing the roster over it is needless.
        # needless.
        # needless.
        if handler is not None:
            try:
                self.store.mutate(lambda state: self._heartbeat(state, request_meta))
            except Exception:  # noqa: BLE001 - heartbeat never drops the tool
                pass
        else:
            raise ProtocolError(f"unknown tool {name!r}")
        try:
            return handler(self, arguments)
        except ProtocolError as exc:
            self.store.append_transcript("refusal", self.role, tool=name, reason=exc.message)
            raise

    def tool_pair_digest(self, budget: int = DIGEST_TOKEN_BUDGET) -> dict[str, Any]:
        """The pair-state slice under a hard token cap — instead of reading history.

        Orientation in the pair costs 3724 tokens reading `pair_state`, `pair_task`
        and `pair_transcript`. This call fits the default `budget`, answering "what
        we decide now, what awaits me, what was decided last" with nothing read from
        history.

        History still reads on top — the digest never cancels the journal, it just
        lets you skip paying for it to learn whether to read it.
        """
        text = build_digest(self.store, self.role, budget)
        return {
            "digest": text,
            "tokens": estimate_tokens(text),
            "budget": budget,
            "note_kind": "verdict-* for the last verdict, "
                         "full text — in `pair_note_read`",
        }

    def tool_pair_transcript(self, limit: int = 20) -> dict[str, Any]:
        """Read the append-only audit trail of everything both agents did."""
        entries = self.store.read_transcript(limit)
        chain = verify_transcript_chain(entries, anchored=bool(limit))
        return {"entries": entries, "count": len(entries), "chain": chain}


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --------------------------------------------------------------------- tools

def tool_definitions() -> list[dict[str, Any]]:
    role_note = "Your own role is fixed by configuration and cannot be overridden per call."
    return [
        {
            "name": "pair_deliver",
            "description": (
                "Type a message directly into the peer's prompt in its own tmux pane, as if the "
                "user had typed it. The peer wakes by itself and reads it as its next instruction. "
                "Chunks the text, verifies it landed, and erases it if not. Refuses when the peer's "
                "composer is busy rather than overtyping. Use this when you need the peer to act "
                "now rather than when it next thinks to call pair_read."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "to": {"type": "string", "description": "The peer's role."},
                    "text": {"type": "string", "description": "What to type. Plain text, one paragraph."},
                    "wait_for_empty": {
                        "type": "number",
                        "description": "Seconds to wait for a busy composer to clear. 0 refuses immediately.",
                        "default": 0,
                    },
                    "submit": {
                        "type": "boolean",
                        "description": "Press Enter after typing. false leaves the text in the composer unsent.",
                        "default": True,
                    },
                },
                "required": ["to", "text"],
            },
        },
        {
            "name": "pair_state",
            "description": (
                "Read the shared collaboration state: the task, who holds write authority, the current "
                "phase, the roster of participating roles, and how many messages you have unread."
            ),
            "inputSchema": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "pair_ping",
            "description": (
                "Cheap sign of life: moves last_seen like any call, but answers "
                "with three fields instead of the full ~939-token state. "
                "Send it yourself every few minutes while working quietly; "
                "the supervisor then tells 'silent but pinging' (working) "
                "apart from 'silent and not pinging' (stuck)."
            ),
            "inputSchema": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "pair_propose",
            "description": (
                "Propose a change in one call instead of six steps "
                "(reserve, write diff, seal, send path+hash, verify, applied). "
                "The server builds the unified diff itself (git diff HEAD over "
                "your worktree files), seals it, and delivers path+hash to the "
                "writer with a review request. Non-writers only; the writer "
                "edits files directly. The manual path stays for exotic cases."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "files": {"type": "array", "items": {"type": "string"},
                              "description": "Worktree paths with your changes."},
                    "subject": {"type": "string", "description": "Optional review subject."},
                },
                "required": ["files"],
            },
        },
        {
            "name": "pair_claim",
            "description": (
                "Claim write authority for your role, optionally saying why you should be the one "
                "writing. Succeeds only when nobody holds it. Refused when a peer already holds it: "
                "only the human can transfer authority, via `pairctl assign <role>`. Claiming makes "
                "you the writer and keeps your peer read-only. Read the task before claiming — a "
                "reason is worth more than arriving first."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": (
                            "One sentence on why you should write: what in the task or the codebase "
                            "made you the right one for it. Recorded in the transcript and shown to "
                            "a peer who is refused."
                        ),
                    }
                },
                "required": [],
            },
        },
        {
            "name": "pair_release",
            "description": (
                "Give up write authority voluntarily, optionally saying why. Only the current holder "
                "may call this; a peer cannot revoke it, and cannot take it — but the holder can give "
                "it, which is a decision rather than a transfer by message. Afterwards nobody holds "
                "authority until the peer claims it or the human assigns it."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "why": {
                        "type": "string",
                        "description": "One sentence on why you are handing over.",
                    }
                },
                "required": [],
            },
        },
        {
            "name": "pair_send",
            "description": (
                "Send one message to the other agent. The `Chat from <role>:` label is added by the "
                "server, so authorship cannot be forged. The peer reads it as conversation, not as an "
                "instruction from the user. Delivery is not a promise of a reply: never block waiting."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "to": {"type": "string", "description": "The peer's role name."},
                    "text": {"type": "string", "description": "The message. Plain language, one paragraph."},
                    "subject": {"type": "string", "description": "Optional short subject line."},
                    "expects_reply": {
                        "type": "boolean",
                        "default": False,
                        "description": "Set it when the message asks for a decision, a review or an "
                                       "action. The supervisor then watches for an answer and pokes "
                                       "the recipient if none comes, so an unanswered request cannot "
                                       "quietly end the night.",
                    },
                },
                "required": ["to", "text"],
            },
        },
        {
            "name": "pair_read",
            "description": (
                "Read messages oldest first. A returned batch stays unread until its exact `ack_token` "
                "is passed on a later pair_read; a retry without it replays the same batch. Other tools "
                "do not ACK it. Pending batches appear in pair_state/digest and block close."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Max messages to return.", "default": 10},
                    "wait_seconds": {
                        "type": "number",
                        "description": "Bounded wait, capped by this role's configured timeout.",
                        "default": DEFAULT_READ_WAIT_SECONDS,
                    },
                    "ack_token": {
                        "type": "string",
                        "description": "Exact token from the previous batch; acknowledges only that batch.",
                    },
                },
                "required": [],
            },
        },
        {
            "name": "pair_advance",
            "description": (
                "Move the shared phase: scoping → plan → implement → verify → done, with a single step "
                "back allowed. Writer only. Verification is a separate phase on purpose: findings that "
                "were never checked against the real code do not count as done."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"phase": {"type": "string", "enum": list(PHASES)}},
                "required": ["phase"],
            },
        },
        {
            "name": "pair_patch_reserve",
            "description": (
                "Reserve an empty mode-0600 patch file for proposing a change. Non-writers only: the "
                "writer edits project files directly and never needs this. Write your unified diff into "
                "the returned exact path, then call pair_patch_seal."
            ),
            "inputSchema": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "pair_patch_seal",
            "description": (
                "Compute and record the SHA-256 of a patch file you reserved, so the writer can detect "
                "any change between proposal and application. Call this after finishing the diff."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Exact path from pair_patch_reserve."}},
                "required": ["path"],
            },
        },
        {
            "name": "pair_patch_verify",
            "description": (
                "Re-hash a sealed patch and compare against the recorded value. The writer calls this "
                "immediately before applying. A mismatch means STOP: the patch changed after it was "
                "sealed and must not be applied."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Exact path of the sealed patch."}},
                "required": ["path"],
            },
        },
        {
            "name": "pair_patch_release",
            "description": (
                "Delete one of your own patch files, by exact path. Glob patterns are rejected: both "
                "agents share the patch directory and either may be holding a file you cannot see."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Exact path. No globs."}},
                "required": ["path"],
            },
        },
        {
            "name": "pair_task",
            "description": (
                "The shared task list. Add what you found, close what is finished. The round "
                "cannot close while any task is open, so a task added mid-round cannot be "
                "forgotten: either it gets done, or it gets dropped with a reason on the "
                "record. The critic adds tasks it discovered; only the writer closes them, "
                "because implementing them is the writer's job (PROTOCOL.md 4)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["add", "done", "drop", "list"]},
                    "id": {"type": "integer",
                           "description": "Which task, for done/drop. Omit for add and list."},
                    "text": {"type": "string", "description": "What to do. Required for add."},
                    "reason": {"type": "string",
                               "description": "Required for drop, and it may not be empty."},
                },
                "required": ["action"],
            },
        },
        {
            "name": "pair_evidence",
            "description": (
                "Record a machine-checkable fact about this round: the test suite with its "
                "count and exit code, lint, typecheck, a commit sha, a PR URL. In semi and "
                "auto the round cannot be closed without it, so 'it seems to work' has "
                "nowhere to go. ok=false records a failure honestly instead of hiding it, and "
                "blocks closing until it is resolved or explained in a note."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": list(EVIDENCE_KINDS)},
                    "value": {"type": "string",
                              "description": "The machine's own output, not your verdict. e.g. '117 passed, exit 0'."},
                    "ok": {"type": "boolean", "default": True,
                           "description": "false records a failure. It stays visible and blocks closing."},
                    "note": {"type": "string", "description": "Optional: how it was produced."},
                    "task": {"type": "integer",
                             "description": "Which task this proves. Defaults to the single open one."},
                },
                "required": ["kind", "value"],
            },
        },
        {
            "name": "pair_patch_applied",
            "description": (
                "Record the outcome of applying a sealed patch. Writer only, and only after "
                "pair_patch_verify matched. Without this the protocol had no way to say a "
                "patch had been applied, so a round could close over a proposal nobody ever "
                "touched. ok=false means the apply failed and stays unresolved."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Exact path of the sealed patch."},
                    "ok": {"type": "boolean", "default": True,
                           "description": "false if the apply failed. The patch then still blocks closing."},
                    "note": {"type": "string", "description": "Optional: hunk, commit, what went wrong."},
                },
                "required": ["path"],
            },
        },
        {
            "name": "pair_note",
            "description": (
                "Append a durable note to the shared notes directory under a slug kind, for example "
                "plan, decision, finding, verdict, handoff. This is the memory that survives a context "
                "reset, so anything the peer must still know goes here rather than into the chat."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "description": "Slug, e.g. plan, decision, finding, verdict."},
                    "text": {"type": "string", "description": "Markdown body."},
                    "head": {"type": "string", "description": "Optional explicit revision (SHA) the verdict binds to instead of the live tree. Verdict kinds only; staleness still reads content."},
                },
                "required": ["kind", "text"],
            },
        },
        {
            "name": "pair_digest",
            "description": (
                "Cheap orientation: task, mode, phase, open tasks, obligations, last commit and "
                "last verdict, within a hard token budget. Replaces reading pair_state, "
                "pair_task and pair_transcript to find out where the round stands (3724 tokens "
                "measured). Read history only when the digest says you need it."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "budget": {
                        "type": "integer",
                        "description": f"Token ceiling for the whole answer, {DIGEST_TOKEN_BUDGET} by default.",
                    },
                },
            },
        },
        {
            "name": "pair_note_index",
            "description": (
                "List every note with its size, entry count and freshness, largest first. Use this "
                "before deciding what to read: a single note can cost ~19000 tokens in full, and this "
                "listing costs none of the content. Read notes with pair_note_read, not with a file read."
            ),
            "inputSchema": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "pair_note_read",
            "description": (
                "Read the TAIL of a note, never the whole file. Notes only ever grow at the tail, so "
                "the recent entries are what matter. The answer is cut at an entry boundary and says "
                "whether it was truncated, so a truncated tail is never mistaken for the full note."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "description": "Note slug, for example verdict or plan."},
                    "tail_lines": {
                        "type": "integer",
                        "description": (
                            f"Approximate tail size to return. Default {NOTE_READ_DEFAULT}, "
                            f"capped at {NOTE_READ_MAX}."
                        ),
                        "minimum": 1,
                    },
                    "limit": {"type": "integer", "description": "Alias for tail_lines.", "minimum": 1},
                },
                "required": ["kind"],
            },
        },
        {
            "name": "pair_transcript",
            "description": (
                "Read the append-only audit trail: every message, claim, phase change, patch seal and "
                "hash verification either agent performed. Use it to reconstruct what actually happened."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"limit": {"type": "integer", "description": "Tail size.", "default": 40}},
                "required": [],
            },
        },
        {
            "name": "pair_unblock",
            "description": (
                "Check if a peer agent's terminal pane is blocked waiting on an interactive "
                "permission prompt (e.g. [y/n] or Allow). The prompt is confirmed only if it "
                "matches the permission allowlist — read-only tools by default — and a refusal "
                "is reported rather than silently pressed yes. Anything that writes, runs or "
                "deletes waits for the human."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "peer": {
                        "type": "string",
                        "description": "Role name of the peer to check and unblock (optional if only one peer is registered).",
                        "default": "",
                    },
                },
                "required": [],
            },
        },
    ]


HANDLERS = {
    "pair_state": lambda session, args: session.tool_pair_state(),
    "pair_ping": lambda session, args: session.tool_pair_ping(),
    "pair_deliver": lambda session, args: session.tool_pair_deliver(**args),
    "pair_claim": lambda session, args: session.tool_pair_claim(**args),
    "pair_release": lambda session, args: session.tool_pair_release(**args),
    "pair_send": lambda session, args: session.tool_pair_send(**args),
    "pair_read": lambda session, args: session.tool_pair_read(**args),
    "pair_advance": lambda session, args: session.tool_pair_advance(**args),
    "pair_patch_reserve": lambda session, args: session.tool_pair_patch_reserve(),
    "pair_patch_seal": lambda session, args: session.tool_pair_patch_seal(**args),
    "pair_patch_verify": lambda session, args: session.tool_pair_patch_verify(**args),
    "pair_propose": lambda session, args: session.tool_pair_propose(**args),
    "pair_patch_release": lambda session, args: session.tool_pair_patch_release(**args),
    "pair_patch_applied": lambda session, args: session.tool_pair_patch_applied(**args),
    "pair_task": lambda session, args: session.tool_pair_task(**args),
    "pair_evidence": lambda session, args: session.tool_pair_evidence(**args),
    "pair_note": lambda session, args: session.tool_pair_note(**args),
    "pair_note_index": lambda session, args: session.tool_pair_note_index(),
    "pair_note_read": lambda session, args: session.tool_pair_note_read(**args),
    "pair_digest": lambda session, args: session.tool_pair_digest(**args),
    "pair_transcript": lambda session, args: session.tool_pair_transcript(**args),
    "pair_unblock": lambda session, args: session.tool_pair_unblock(**args),
}


# ----------------------------------------------------------------- MCP server


class MCPServer:
    def __init__(self, session: PairSession) -> None:
        self.session = session
        self.initialized = False
        self._subscriptions: dict[str, str] = {}
        self._notify_lock = threading.RLock()
        self._watch_stop = threading.Event()
        self._watch_thread: threading.Thread | None = None
        # Transport abstraction (round 106 slice D). The framing protocol above
        # is stdio-shaped only because its source and sink are; binding the loop
        # to sys.stdin/sys.stdout is what tied the server's lifetime to the CLI
        # that spawned it. A socket transport keeps the same protocol and lets
        # the SERVER outlive the CLI: the client can die, reconnect, and continue.
        self._in: Any = None
        self._out: Any = None

    def send(self, payload: dict[str, Any]) -> None:
        # stdout is the shared resource of the main loop and the watcher thread: with no
        # framing-lock two interleaved json.dumps break the stream, and the client reads garbage.
        with self._notify_lock:
            sink = self._out if self._out is not None else sys.stdout
            sink.write(json.dumps(payload, ensure_ascii=False) + "\n")
            sink.flush()

    def reply(self, request_id: Any, result: dict[str, Any]) -> None:
        self.send({"jsonrpc": "2.0", "id": request_id, "result": result})

    def error(self, request_id: Any, code: int, message: str, data: Any = None) -> None:
        error: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        self.send({"jsonrpc": "2.0", "id": request_id, "error": error})

    def run(self) -> None:
        log(f"ready role={self.session.role} state={self.session.store.root}")
        self.start_watcher()
        # readline() rather than iteration: iterating sys.stdin uses a read-ahead
        # buffer that withholds complete lines until the buffer fills, which on a
        # pipe deadlocks against a client waiting for our reply.
        source = self._in if self._in is not None else sys.stdin
        while True:
            raw = source.readline()
            if not raw:
                break
            raw = raw.strip()
            if not raw:
                continue
            try:
                message = json.loads(raw)
            except json.JSONDecodeError as exc:
                self.error(None, -32700, f"parse error: {exc}")
                continue
            if isinstance(message, list):
                for item in message:
                    self.dispatch(item)
                continue
            self.dispatch(message)
        log("stdin closed, exiting")
        self.stop_watcher()

    def serve_socket(self, path: str, once: bool = True) -> int:
        """Serve the SAME protocol over a unix socket, outliving any one client.

        The defect this answers (round 106 slice D): bound to stdio, the server
        IS the CLI's child — measured ppid ties, omp's server under bun 59561,
        opencode's under its own parent — so killing either side killed both, and
        a drop meant relaunching the CLI and losing its session context.

        Here the socket is the server's public face. A client connects, speaks
        newline-delimited JSON-RPC 2.0 — the identical framing — and may die; the
        server stays up and takes the next connection. The role lock is held for
        the server's whole life, exactly as with stdio: one live process per role
        per state dir, and the kernel releases it if this process dies.

        `once=True` serves exactly one client then exits — the shape the
        round-trip test uses. `once=False` loops, which is what a daemon does.
        """
        import os
        import socket as _socket
        import threading

        sock_path = str(path)
        os.makedirs(os.path.dirname(sock_path) or ".", exist_ok=True)
        try:
            os.unlink(sock_path)
        except FileNotFoundError:
            pass
        listener = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        listener.bind(sock_path)
        listener.listen(4)
        log(f"socket ready role={self.session.role} state={self.session.store.root} "
            f"path={sock_path}")
        self.start_watcher()
        try:
            while True:
                conn, _ = listener.accept()
                reader = conn.makefile("r", encoding="utf-8", newline="\n")
                writer = conn.makefile("w", encoding="utf-8", newline="\n")
                self._in, self._out = reader, writer
                # A lock held for ONE client at a time: two CLIs talking to one
                # role must not interleave frames on different connections.
                try:
                    while True:
                        raw = reader.readline()
                        if not raw:
                            break
                        raw = raw.strip()
                        if not raw:
                            continue
                        try:
                            message = json.loads(raw)
                        except json.JSONDecodeError as exc:
                            self.error(None, -32700, f"parse error: {exc}")
                            continue
                        if isinstance(message, list):
                            for item in message:
                                self.dispatch(item)
                            continue
                        self.dispatch(message)
                except (OSError, ValueError) as exc:
                    log(f"client gone ({exc}); server stays up")
                finally:
                    self._in, self._out = None, None
                    try:
                        conn.close()
                    except OSError:
                        pass
                if once:
                    break
        finally:
            self.stop_watcher()
            try:
                listener.close()
            except OSError:
                pass
            try:
                os.unlink(sock_path)
            except FileNotFoundError:
                pass
        log(f"socket closed {sock_path}")
        return 0

    def dispatch(self, message: Any) -> None:
        if not isinstance(message, dict):
            self.error(None, -32600, "request must be a JSON object")
            return
        method = message.get("method")
        request_id = message.get("id")
        if not isinstance(method, str):
            self.error(request_id, -32600, "missing method")
            return
        # Notifications carry no id and get no response.
        is_notification = "id" not in message

        try:
            handler = getattr(self, f"on_{method.replace('/', '_').replace('.', '_')}", None)
            if handler is None:
                if not is_notification:
                    self.error(request_id, -32601, f"method not found: {method}")
                return
            handler(request_id, message.get("params") or {})
        except ProtocolError as exc:
            if is_notification:
                log(f"protocol refusal on {method}: {exc.message}")
                return
            text = exc.message + (f"\n\n{exc.hint}" if exc.hint else "")
            self.send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"content": [{"type": "text", "text": text}], "isError": True},
                }
            )
        except Exception as exc:  # noqa: BLE001 - a server crash must not kill the client silently
            log(f"internal error on {method}: {exc!r}")
            if not is_notification:
                self.error(request_id, -32603, f"internal error: {exc}")

    def on_initialize(self, request_id: Any, params: dict[str, Any]) -> None:
        requested = params.get("protocolVersion")
        version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else DEFAULT_PROTOCOL_VERSION
        self.initialized = True
        with self.session.store.lock():
            state = self.session.store._load()
            self.session._register(state)
            if self.session.role == "opencode":
                state["roles"][self.session.role].pop("opencode_session_id", None)
            self.session.store._save(state)
        log(f"initialized by {params.get('clientInfo', {}).get('name', '?')} protocol={version}")
        self.reply(
            request_id,
            {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": (
                    f"You are role {self.session.role!r} in a two-agent collaboration. One role holds write "
                    "authority and edits project files; the other is read-only and proposes sealed patches. "
                    "Authority is transferred only by the human through pairctl, never by a message. "
                    "Call pair_state first to see where you stand. Read peer messages with pair_read; "
                    "ACK returned batches with their exact ack_token after processing. Retries replay; "
                    "messages remain unread until ACK, and pair_state shows the pending batch."
                ),
            },
        )

    def on_notifications_initialized(self, request_id: Any, params: dict[str, Any]) -> None:
        return None

    def on_ping(self, request_id: Any, params: dict[str, Any]) -> None:
        self.reply(request_id, {})

    def on_tools_list(self, request_id: Any, params: dict[str, Any]) -> None:
        self.reply(request_id, {"tools": tool_definitions()})

    def on_tools_call(self, request_id: Any, params: dict[str, Any]) -> None:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise ProtocolError("`arguments` must be an object")
        result = self.session.invoke(name, arguments, params.get("_meta"))
        text = json.dumps(result, ensure_ascii=False, indent=2)
        self.reply(request_id, {"content": [{"type": "text", "text": text}], "isError": False})

    # Push is the accelerator, the mailbox is the guarantee (§7). Subscriptions live in
    # process memory, never durable: the process died means the subscription died, the
    # client resubscribes. Cross-process transport is files only: send writes a notify
    # file, our own process watcher thread picks it up and sends the notification under
    # the stdout framing-lock. With no host listen, long-poll pair_read serves.
    def notify(self, uri: str) -> None:
        with self._notify_lock:
            for sub_id, sub_uri in list(self._subscriptions.items()):
                if sub_uri == uri:
                    self.send({"jsonrpc": "2.0", "method": "notifications/resources/updated",
                               "params": {"uri": uri, "subscriptionId": sub_id}})

    def start_watcher(self) -> None:
        """The file-transport watcher thread: every 0.3s drains our resource-update queue
        (notify files from any process send) and notifies live subscriptions. A
        daemon thread, never durable: the server died means the queue stays in files,
        subscriptions gone, the client resubscribes."""
        def loop() -> None:
            while not self._watch_stop.is_set():
                try:
                    uris = self.session.store.drain_resource_updates(self.session.role)
                    for uri in uris:
                        self.notify(uri)
                except Exception as exc:  # noqa: BLE001 - the watcher must never kill the server
                    log(f"notify watcher error: {exc!r}")
                self._watch_stop.wait(0.3)
        self._watch_thread = threading.Thread(target=loop, daemon=True)
        self._watch_thread.start()

    def stop_watcher(self) -> None:
        self._watch_stop.set()

    def on_resources_list(self, request_id: Any, params: dict[str, Any]) -> None:
        role = self.session.role
        self.reply(request_id, {"resources": [
            {"uri": f"mailbox://{role}", "name": f"Incoming roles {role}",
             "mimeType": "application/json"},
            {"uri": "obligations://", "name": "Open answer waits",
             "mimeType": "application/json"},
        ]})

    def on_resources_templates_list(self, request_id: Any, params: dict[str, Any]) -> None:
        self.reply(request_id, {"resourceTemplates": [
            {"uriTemplate": "mailbox://{role}", "name": "Incoming roles",
             "mimeType": "application/json"},
        ]})

    def on_resources_subscribe(self, request_id: Any, params: dict[str, Any]) -> None:
        uri = params.get("uri", "")
        role = self.session.role
        if uri not in (f"mailbox://{role}", "obligations://"):
            raise ProtocolError(f"unknown resource {uri!r}: subscribe only to mailbox://{role} and obligations://")
        sub_id = f"sub-{uuid.uuid4().hex[:8]}"
        self._subscriptions[sub_id] = uri
        self.reply(request_id, {"subscriptionId": sub_id})

    def on_resources_unsubscribe(self, request_id: Any, params: dict[str, Any]) -> None:
        self._subscriptions.pop(params.get("id", ""), None)
        self.reply(request_id, {})

    def on_prompts_list(self, request_id: Any, params: dict[str, Any]) -> None:
        self.reply(request_id, {"prompts": []})

    def on_logging_setLevel(self, request_id: Any, params: dict[str, Any]) -> None:
        self.reply(request_id, {})




def main(argv: list[str] | None = None) -> int:
    """The single entry point: the MCP server or a `pairctl` command.

    This file name and path must never move: `serve.sh`, `install.sh` and the MCP
    config of all four roles depend on them. So `pairctl` itself lives in
    `pairctl_cli.py`, called from here, not the reverse.

    `run_ctl` imports lazily: the `pairctl_*` modules import `server`, and top level
    would close an import cycle. Inside the function no cycle forms, because by then
    `server` already loads.
    """
    from pairctl_cli import build_parser, run_ctl
    from pairctl_read import extract_pane, extract_state

    raw_args = list(sys.argv[1:] if argv is None else argv)
    state_dir, rest = extract_state(raw_args)
    explicit_pane, rest = extract_pane(rest)
    # The socket flag is ours, not the CLI parser's: it selects the TRANSPORT
    # (round 106 slice D) and leaves every protocol argument untouched.
    socket_path = ""
    if "--socket" in rest:
        i = rest.index("--socket")
        if i + 1 < len(rest) and not rest[i + 1].startswith("--"):
            socket_path = rest[i + 1]
            rest = rest[:i] + rest[i + 2:]
        else:
            socket_path = "auto"
            rest = rest[:i] + rest[i + 1:]
    args = build_parser().parse_args(rest)
    if args.ctl_command:
        return run_ctl(args, state_dir)
    if not args.role:
        print("error: --role is required when running as an MCP server", file=sys.stderr)
        return 2
    store = Store(state_dir)
    try:
        _role_handle = store.acquire_role_lock(args.role)
    except ProtocolError as exc:  # remedy in hint, else an actionless traceback
        print(exc.message + (f"\n{exc.hint}" if exc.hint else ""), file=sys.stderr)
        return 1
    server = MCPServer(PairSession(args.role, store, pane=explicit_pane))
    if not socket_path:
        server.run()
        return 0
    if socket_path == "auto":
        socket_path = str(store.root / f".server-{args.role}.sock")
    # `once=False` for the daemon: the whole point is that the transport outlives
    # one client, so a CLI may die and reconnect with its context intact.
    # PAIR_SOCKET_ONCE=1 serves exactly one client — the shape the round-trip
    # test needs, and never the shape a live daemon uses.
    once = os.environ.get("PAIR_SOCKET_ONCE", "0") == "1"
    return server.serve_socket(socket_path, once=once)


if __name__ == "__main__":
    sys.exit(main())