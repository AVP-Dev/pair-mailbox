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
Human control surface: ``server.py ctl <command>``.
"""

from __future__ import annotations

import argparse
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
import time
import uuid

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# composer.py лежит рядом и импортируется лениво, внутри pair_deliver.
# Путь добавляем здесь, после того как Path уже есть.
sys.path.insert(0, str(Path(__file__).resolve().parent))

# MCP protocol revisions this server understands. We echo the client's requested
# revision back when we know it, because clients reject a downgrade they did not
# ask for.
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_PROTOCOL_VERSION = "2024-11-05"

SERVER_NAME = "pair-mailbox"
SERVER_VERSION = "1.0.0"

MAX_READ_WAIT_SECONDS = 60
DEFAULT_READ_WAIT_SECONDS = 0.0
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


class ProtocolError(Exception):
    """A request that is well-formed MCP but violates the collaboration rules."""

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


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
        self.state_path = root / "state.json"
        self.transcript_path = root / "transcript.jsonl"
        self.lock_path = root / ".lock"
        for directory in (self.root, self.mailbox_root, self.notes_root,
                          self.patches_root, self.obligations_root):
            directory.mkdir(parents=True, exist_ok=True)

    def default_state(self) -> dict[str, Any]:
        return {
            "task": "",
            "mode": "fix",
            "writer": None,
            "phase": "scoping",
            "round": 0,
            "created_at": utcnow(),
            "updated_at": utcnow(),
            "roles": {},
            "seen": {},
            "patches": {},
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
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with self.lock():
            with self.transcript_path.open("a", encoding="utf-8") as handle:
                handle.write(line)

    def read_transcript(self, limit: int = 0) -> list[dict[str, Any]]:
        if not self.transcript_path.exists():
            return []
        entries: list[dict[str, Any]] = []
        with self.transcript_path.open("r", encoding="utf-8") as handle:
            for raw in handle:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entries.append(json.loads(raw))
                except json.JSONDecodeError:
                    continue
        return entries[-limit:] if limit else entries


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


def is_pid_alive(pid: Any) -> bool:
    """True if pid is an integer of a currently running process."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError as err:
        return err.errno == errno.EPERM


class PairSession:
    """One agent's view of the shared state, bound to a role at construction."""

    def __init__(self, role: str, store: Store) -> None:
        if not role or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", role):
            raise SystemExit(f"invalid --role {role!r}: use letters, digits, dot, dash, underscore")
        self.role = role
        self.store = store

    # ---------------------------------------------------------------- helpers

    def _register(self, state: dict[str, Any]) -> None:
        roles = state.setdefault("roles", {})
        pane = os.environ.get("TMUX_PANE")
        current_pid = os.getpid()

        # If this role already existed with a different living PID, record collision
        existing = roles.get(self.role)
        if existing and existing.get("pid") and existing["pid"] != current_pid:
            if is_pid_alive(existing["pid"]):
                self.store.append_transcript(
                    "role_collision",
                    self.role,
                    current_pid=current_pid,
                    existing_pid=existing["pid"],
                )

        # If another role holds the same tmux pane, check if that other process is dead
        if pane:
            for other_name, other_info in list(roles.items()):
                if other_name != self.role and other_info.get("pane") == pane:
                    other_pid = other_info.get("pid")
                    if other_pid and not is_pid_alive(other_pid):
                        other_info.pop("pane", None)
                    elif other_pid and is_pid_alive(other_pid):
                        self.store.append_transcript(
                            "pane_collision",
                            self.role,
                            pane=pane,
                            other_role=other_name,
                            other_pid=other_pid,
                        )

        entry = roles.setdefault(self.role, {"first_seen": utcnow(), "reads": 0, "sends": 0})
        entry["last_seen"] = utcnow()
        entry["pid"] = current_pid
        if pane:
            entry["pane"] = pane

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
        """Постоянная расстановка ролей, заданная человеком.

        Без неё каждый раунд начинается гонкой, а гонка — это время впустую и
        повод остановиться посреди работы. С ней всё известно заранее: один
        всегда пишет, второй всегда ревьюит, и ни один не тратит ход на спор.

        Это решение человека, поэтому оно и хранится как решение человека, а не
        как побочный эффект pair_claim.
        """
        policy = state.get("policy")
        if isinstance(policy, dict) and policy.get("writer"):
            return policy
        # Глобальная расстановка действует, только если её писатель РЕАЛЬНО
        # работает в этом проекте. Иначе она вредна: назначенной роли здесь нет,
        # она не появится никогда, и право записи не возьмёт уже никто — работа
        # встанет с причиной, которую никто не формулирует.
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

        writer = state.get("writer")
        policy_writer = self._policy(state).get("writer")
        # `_load` merges whatever was on disk over the defaults, so a hand-edited or
        # newer-server state can carry a mode this build does not know. Reading it as a
        # fix round is the safe direction: an unrecognised value must never be a licence
        # to edit, and must never be able to crash an agent's first pair_state call.
        mode = state.get("mode", "fix")
        if mode not in MODES:
            mode = "fix"
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
            "task": state.get("task", ""),
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
                    "Nothing in the project gets changed on purpose. Do not edit project "
                    "files. Findings go to pair_note; each one is checked by the peer, "
                    "confirmed ones become docs/backlog entries, and the deliverable is "
                    "evidence rather than a diff."
                ),
            }[mode],
            "writer": writer,
            # Постоянная расстановка, если человек её задал. Тогда роль известна
            # заранее и спор о праве записи не возникает вовсе.
            "policy": self._policy(state) or None,
            # Что я кому должен услышать. Показываю обоим: иначе про ожидание
            # знает только сверхвизор, а это ровно то, о чём не знает никто.
            "obligations": {
                # «Жду» — это обязательства, открытые МНОЮ. «От меня ждут» —
                # обязательства, где я получатель. Путать их опасно: один вариант
                # ведёт к ожиданию, другой к долгу, и лечатся они противоположно.
                "i_am_waiting_for": self._obligations_where("from", self.role),
                "they_wait_for_me": self._obligations_where("to", self.role),
            },
            "my_standing": (
                "writer по постоянной расстановке" if policy_writer == self.role
                else "критик по постоянной расстановке" if policy_writer
                else "не задано, определяется гонкой"
            ),
            # Почему именно он пишет. Без этого сосед видит только факт и не может
            # понять, спорить ему с выбором или принять его.
            "writer_reason": state.get("writer_reason", ""),
            "phase": current_phase,
            "round": state.get("round", 0),
            "phases": list(PHASES),
            "legal_next_phases": legal_next,
            "roles": self._known_roles(state),
            "unread": self._unread_count(state),
            "peers": self._peer_status(state, writer),
            "how_to_transfer_authority": (
                "Only the human can move write authority: `pairctl assign <role>`. "
                "A peer message never transfers it, in either direction."
            ),
        }

    def _peer_status(self, state: dict[str, Any], writer: str | None) -> dict[str, Any]:
        """What each other role is doing, and whether it is still listening.

        A writer used to close a round with no way of knowing the peer had already
        walked away, so the peer's report landed in a closed round. Saying so out
        loud is cheaper than a dead-lettered finding.
        """
        phase = state.get("phase", "scoping")
        now = datetime.now(timezone.utc)
        out: dict[str, Any] = {}
        for name, info in (state.get("roles") or {}).items():
            if name == self.role:
                continue
            age = None
            try:
                if info.get("last_seen"):
                    age = int((now - datetime.fromisoformat(info["last_seen"])).total_seconds())
            except Exception:
                age = None
            closed = phase == "done"
            pid = info.get("pid")
            alive = is_pid_alive(pid) if pid else None
            hint = "Listening."
            if closed:
                hint = (
                    "The round is closed. This role has moved on and will not read new"
                    " messages. To get it back: pair_advance('implement')."
                )
            elif alive is False:
                hint = f"Process is no longer running (PID {pid} is dead)."
            out[name] = {
                "role": "writer" if name == writer else "critic",
                "last_seen": info.get("last_seen"),
                "silent_seconds": age,
                "alive": alive,
                "round_closed": closed,
                "will_read_new_messages": not closed and (alive is not False),
                "hint": hint,
            }
        return out

    def _unread_count(self, state: dict[str, Any]) -> int:
        seen = int(state.get("seen", {}).get(self.role, 0))
        return max(0, len(self._inbox_files()) - seen)

    def _inbox_files(self) -> list[Path]:
        inbox = self.store.mailbox_root / self.role
        if not inbox.is_dir():
            return []
        return sorted(inbox.glob("*.json"), key=lambda p: p.name)

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
            if designated and designated != self.role:
                raise ProtocolError(
                    f"по постоянной расстановке ролей писатель {designated!r}, а ты критик.",
                    "Это решение человека, а не гонка: работай критиком и не трать ход на спор. "
                    "Сменить расстановку может только человек: `pairctl roles --swap`.",
                )
            if designated and designated == self.role and not state.get("writer"):
                state["writer"] = self.role
                state["writer_reason"] = "назначено политикой ролей"
                return {
                    "already_writer": False,
                    "writer": self.role,
                    "round": state.get("round", 0),
                    "by_policy": True,
                }
            current = state.get("writer")
            was_closed = bool(state.get("closed_at") or state.get("phase") == "done")
            if current and current != self.role:
                if not self._writer_is_abandoned(state, current) and not was_closed:
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
                if reason.strip():
                    state["writer_reason"] = reason.strip()
                if was_closed:
                    state.pop("closed_at", None)
                    state["phase"] = "implement"
                return {
                    "already_writer": False,
                    "writer": self.role,
                    "round": state["round"],
                    "took_over": True,
                }
            if current == self.role:
                if reason.strip():
                    state["writer_reason"] = reason.strip()
                if was_closed:
                    state.pop("closed_at", None)
                    state["phase"] = "implement"
                    state["round"] = int(state.get("round", 0)) + 1
                    return {"already_writer": False, "writer": self.role, "round": state["round"], "took_over": False}
                return {"already_writer": True, "writer": self.role, "round": int(state.get("round", 0)), "took_over": False}
            state["writer"] = self.role
            state["round"] = int(state.get("round", 0)) + 1
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
            # Не выиграл гонку, а получил роль по решению человека: разница важна,
            # потому что спор о роли тут неуместен.
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

    def _ring(self, target: str, kind: str, detail: str) -> dict[str, Any]:
        """Knock on the peer's tmux pane so a sleeping agent wakes itself.

        An idle process cannot be woken any other way: it has to be handed the keys.
        That is the whole reason this exists. The body is already in the mailbox or in
        a note, so what gets typed is a short trigger, which is safe to push into a
        composer where a whole paragraph would be mangled.
        """
        if os.environ.get("PAIR_DISABLE_RING") == "1":
            return {"rung": False, "why": "ring disabled by PAIR_DISABLE_RING"}
        state = self.store.read()
        pane = (state.get("roles", {}).get(target) or {}).get("pane")
        if not pane:
            return {"rung": False, "why": "нет панели, сосед вне tmux"}
        if pane == os.environ.get("TMUX_PANE"):
            return {"rung": False, "why": "не звоним в собственную панель"}
        tmux = shutil.which("tmux")
        if not tmux:
            return {"rung": False, "why": "tmux не найден в PATH"}
        probe = subprocess.run(
            [tmux, "display-message", "-p", "-t", pane, "#{pane_dead}"],
            capture_output=True, text=True, timeout=5,
        )
        if probe.returncode != 0 or probe.stdout.strip() == "1":
            return {"rung": False, "why": "панель соседа мертва"}
        from composer import capture, composer_line, is_composer_empty
        deadline = time.monotonic() + 1.5
        while True:
            line = composer_line(capture(tmux, pane))
            if is_composer_empty(line):
                break
            if time.monotonic() >= deadline:
                return {"rung": False, "why": "композер соседа занят, не перебиваем"}
            time.sleep(0.3)
        prompt = f"pair: {kind} от {self.role} — {detail}. Прочитай pair_read."
        if len(prompt) > 150:
            prompt = prompt[:147] + "..."
        subprocess.run([tmux, "send-keys", "-t", pane, "-l", prompt],
                       capture_output=True, text=True, timeout=5)
        subprocess.run(["tmux", "send-keys", "-t", pane, "Enter"],
                       capture_output=True, text=True, timeout=5)
        self.store.append_transcript("doorbell", self.role, to=target, bell_kind=kind, pane=pane)
        return {"rung": True, "pane": pane, "prompt": prompt}

    def tool_pair_unblock(self, peer: str = "") -> dict[str, Any]:
        """Check if a peer's tmux pane is waiting on an interactive permission prompt (e.g. [y/n]) and approve it.

        Use when the peer agent has stalled or silent_seconds is growing, to prevent sessions from blocking on CLI prompts.
        """
        from composer import _tmux, pane_alive, resolve_permission_prompt

        target = peer.strip()
        state = self.store.read()
        roles = state.get("roles", {})
        if not target:
            candidates = [r for r in roles if r != self.role]
            if len(candidates) == 1:
                target = candidates[0]
            elif not candidates:
                return {"unblocked": False, "why": "нет зарегистрированных соседей", "prompt": ""}
            else:
                raise ProtocolError("`peer` is required when multiple peers are registered.")
        if target == self.role:
            raise ProtocolError("cannot unblock own role.")
        pane = (roles.get(target) or {}).get("pane")
        if not pane:
            return {"unblocked": False, "why": f"у {target} нет панели tmux", "prompt": ""}
        try:
            tmux = _tmux()
        except Exception as exc:
            return {"unblocked": False, "why": str(exc), "prompt": ""}
        matched = resolve_permission_prompt(tmux, pane)
        if matched:
            self.store.append_transcript("unblock", self.role, target=target, pane=pane, prompt=matched)
        return {
            "unblocked": bool(matched),
            "peer": target,
            "pane": pane,
            "prompt": matched or "",
            "status": f"Подтверждение [y] отправлено: {matched}" if matched else "Запрос разрешения не обнаружен в панели",
        }

    def tool_pair_deliver(self, to: str, text: str, wait_for_empty: float = 0.0, submit: bool = True) -> dict[str, Any]:
        """Type a message straight into the peer's composer, as if the user typed it.

        The mailbox is passive: a message sits there until the peer thinks to look.
        This is the active path — the text appears in its prompt, so it wakes on its own
        and the exchange reads like two people talking.

        Refuses when the composer is busy rather than overtyping what is being written.
        Chunks the text, verifies the tail landed, and backspaces it out if not.
        """
        from composer import ComposerBusy, deliver

        if not isinstance(to, str) or not to.strip():
            raise ProtocolError("`to` is required.")
        to = to.strip()
        if to == self.role:
            raise ProtocolError("cannot deliver a message to your own role.")
        if not isinstance(text, str) or not text.strip():
            raise ProtocolError("`text` is required.")

        state = self.store.read()
        pane = (state.get("roles", {}).get(to) or {}).get("pane")
        if not pane:
            return {"delivered": False, "why": f"у {to} нет панели tmux, он вне tmux"}
        try:
            result = deliver(pane, text, wait_for_empty=wait_for_empty, submit=submit)
        except ComposerBusy as exc:
            self.store.append_transcript("deliver_busy", self.role, to=to, reason=str(exc))
            send_res = self.tool_pair_send(to=to, text=text, subject="[mailbox fallback: composer busy]")
            return {
                "delivered": False,
                "why": str(exc),
                "fallback_sent": True,
                "seq": send_res.get("seq"),
            }
        self.store.append_transcript("deliver", self.role, to=to, **result)
        result["delivered_to_pane"] = pane
        return result

    def _obligations_where(self, side: str, role: str | None) -> list[dict[str, Any]]:
        """Open obligations, optionally filtered to one role.

        Kept small on purpose: a long list of stale obligations is noise, and the
        supervisor is what actually chases them.
        """
        out: list[dict[str, Any]] = []
        if not self.store.obligations_root.is_dir():
            return out
        for path in sorted(self.store.obligations_root.glob("*.json")):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if role is not None and record.get(side) != role:
                continue
            out.append({"seq": record.get("seq"), "from": record.get("from"),
                        "to": record.get("to"), "opened_at": record.get("opened_at"),
                        "subject": record.get("subject", "")})
        return out[:20]

    def _open_obligation(self, frm: str, to: str, seq: int, subject: str = "") -> dict[str, Any]:
        """Record that `frm` is waiting for an answer from `to`.

        The protocol's blind spot was exactly this: a message asking for a decision was
        indistinguishable from small talk, so a request nobody answered looked like a quiet
        evening. An obligation is a plain file, so the supervisor can read it without
        understanding any of the conversation.
        """
        self.store.obligations_root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = self.store.obligations_root / f"{seq:06d}-{stamp}-{frm}-{to}.json"
        record = {"seq": seq, "from": frm, "to": to, "opened_at": utcnow(), "subject": subject}
        atomic_write_json(path, record)
        self.store.append_transcript("obligation_opened", frm, to=to, seq=seq, subject=subject)
        return record

    def _answer_obligations(self, answerer: str, author: str) -> list[int]:
        """Called when `answerer` writes to `author`: whatever they owed each other closes.

        Any later message counts as the answer, including a fresh request, because two agents
        talking have by definition stopped waiting on each other. Judging the content would be
        guesswork, and a wrong guess here means a real request gets nagged about at 3am.
        """
        closed: list[int] = []
        for path in sorted(self.store.obligations_root.glob("*.json")) if self.store.obligations_root.is_dir() else []:
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if record.get("from") == answerer and record.get("to") == author:
                path.unlink(missing_ok=True)
                closed.append(int(record.get("seq", 0)))
        if closed:
            self.store.append_transcript("obligation_answered", answerer, author=author, seqs=closed)
        return closed

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

        def apply(state: dict[str, Any]) -> dict[str, Any]:
            self._register(state)
            roles = state.setdefault("roles", {})
            roles[self.role]["sends"] = int(roles[self.role].get("sends", 0)) + 1
            if to not in roles:
                # Not fatal: the peer may not have started yet. Recorded for auditing.
                pass
            inbox = self.store.mailbox_root / to
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
            atomic_write_json(inbox / filename, body)
            return {"seq": seq, "known_target": to in roles}

        closed = self._answer_obligations(to, self.role)
        result = self.store.mutate(apply)
        self.store.append_transcript("send", self.role, to=to, seq=result["seq"], subject=subject)
        obligation = None
        if expects_reply:
            obligation = self._open_obligation(self.role, to, seq=result["seq"],
                                               subject=subject or text.strip()[:60])
        ring = self._ring(to, "сообщение", subject or text.strip()[:50])
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

    def tool_pair_read(self, limit: int = 10, wait_seconds: float = DEFAULT_READ_WAIT_SECONDS) -> dict[str, Any]:
        """Read messages addressed to this role.

        The writer never blocks. A writer that waits for its peer while the peer
        waits for the writer is a deadlock, so wait_seconds is forced to 0 for the
        writer. Only a non-writer may wait, and it is still capped.
        """
        try:
            wait = float(wait_seconds)
        except (TypeError, ValueError):
            raise ProtocolError("`wait_seconds` must be a number.") from None
        if wait < 0:
            raise ProtocolError("`wait_seconds` must not be negative.")
        wait = min(wait, MAX_READ_WAIT_SECONDS)

        # Both roles may wait. This used to be forced to zero for the writer, to stop
        # two agents blocking each other, but that killed the channel instead: an agent
        # that cannot wait goes idle and never notices an incoming message, so the human
        # ends up relaying by hand. A bounded wait cannot deadlock, because the wait
        # always expires and the caller must then get back to work.

        deadline = time.monotonic() + wait
        while True:
            messages = self._drain(limit)
            if messages or time.monotonic() >= deadline:
                break
            time.sleep(0.1)

        return {
            "messages": messages,
            "count": len(messages),
            "waited_seconds": round(wait, 2),
            "note": (
                "Waiting is the normal way to hear from your peer. If it came back empty, "
                "call again. You must not answer a status question with a promise to go and "
                "check — read pair_state or pair_transcript and answer with what is true now."
            ),
        }

    def _drain(self, limit: int) -> list[dict[str, Any]]:
        def apply(state: dict[str, Any]) -> list[dict[str, Any]]:
            self._register(state)
            files = self._inbox_files()
            seen = int(state.get("seen", {}).get(self.role, 0))
            if seen > len(files):
                seen = len(files)
            fresh = files[seen : seen + max(0, int(limit))]
            out: list[dict[str, Any]] = []
            successful_count = 0
            for path in fresh:
                try:
                    out.append(json.loads(path.read_text(encoding="utf-8")))
                    successful_count += 1
                except (OSError, json.JSONDecodeError) as exc:
                    self.store.append_transcript("message_unreadable", self.role,
                                                 path=str(path), error=str(exc))
                    break
            if successful_count:
                state.setdefault("seen", {})[self.role] = seen + successful_count
            elif seen != int(state.get("seen", {}).get(self.role, 0)):
                state.setdefault("seen", {})[self.role] = seen
            return out

        return self.store.mutate(apply)

    def tool_pair_advance(self, phase: str) -> dict[str, Any]:
        """Move the shared phase.

        Forward belongs to the writer. One step back belongs to anyone: a critic
        that found something after the writer closed the round must be able to
        reopen it, and reopening is not a write operation.

        Closing is refused over unread mail, because a report the writer never
        read is not a report.
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

        def apply(state: dict[str, Any]) -> None:
            if phase == "done":
                unread = self._unread_count(state)
                if unread > 0:
                    raise ProtocolError(
                        f"{unread} unread message(s) in your inbox; a round cannot be closed over unread mail.",
                        "Run pair_read first. If the peer still needs something, it has not been said yet.",
                    )
            state["phase"] = phase
            if reopening:
                state.pop("closed_at", None)
                state["reopened_by"] = self.role
                state["round"] = int(state.get("round", 0)) + 1
            elif phase == "done":
                state["closed_at"] = utcnow()

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

    def tool_pair_patch_seal(self, path: str) -> dict[str, Any]:
        """Compute and record the hash of a patch this role reserved."""
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

    def tool_pair_patch_release(self, path: str) -> dict[str, Any]:
        """Delete one of this role's own patch files, by exact path."""
        if any(char in path for char in GLOB_CHARS):
            raise ProtocolError(
                f"{path} contains a glob character.",
                "Release patches by exact path. Never clean a directory with a glob: two agents "
                "share it and one of them may hold a file you cannot see.",
            )

        def apply(state: dict[str, Any]) -> bool:
            record = self._owned_patch(state, path)
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

    def tool_pair_note(self, kind: str, text: str) -> dict[str, Any]:
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
        target = self.store.notes_root / f"{kind}.md"
        with self.store.lock():
            with target.open("a", encoding="utf-8") as stream:
                stream.write(f"\n## {utcnow()} — {self.role}\n\n{text.strip()}\n")
        self.store.append_transcript("note", self.role, note_kind=kind, chars=len(text))
        woke = {}
        for other in self.store.read().get("roles", {}):
            if other == self.role:
                continue
            r = self._ring(other, f"заметка/{kind}", text.strip()[:50])
            if r["rung"]:
                woke[other] = r["pane"]
        return {"kind": kind, "path": str(target), "woke_peers": woke}
    def invoke(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Run one tool by name, recording refusals on the audit trail.

        Every refusal lands in the transcript regardless of transport, so the
        trail answers "what did each agent try, and what was it told?" and not
        just "what succeeded". A peer probing for a way around the rules is
        exactly the event worth keeping.
        """
        handler = HANDLERS.get(name)
        if handler is None:
            raise ProtocolError(f"unknown tool {name!r}")
        try:
            return handler(self, arguments)
        except ProtocolError as exc:
            self.store.append_transcript("refusal", self.role, tool=name, reason=exc.message)
            raise

    def tool_pair_transcript(self, limit: int = 40) -> dict[str, Any]:
        """Read the append-only audit trail of everything both agents did."""
        entries = self.store.read_transcript(limit)
        return {"entries": entries, "count": len(entries)}


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
                "Read messages addressed to your role, oldest first. Marks them read. `wait_seconds` "
                "lets a non-writer block briefly for the peer; the writer is always forced to zero, "
                "because two agents waiting on each other is a deadlock. Read once, act, move on."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Max messages to return.", "default": 10},
                    "wait_seconds": {
                        "type": "number",
                        "description": "Bounded wait. Ignored (forced to 0) for the writer.",
                        "default": DEFAULT_READ_WAIT_SECONDS,
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
                },
                "required": ["kind", "text"],
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
                "Check if a peer agent's terminal pane is blocked waiting on an interactive permission prompt "
                "(e.g. [y/n] or Allow) and send 'y' to unblock them."
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
    "pair_deliver": lambda session, args: session.tool_pair_deliver(**args),
    "pair_claim": lambda session, args: session.tool_pair_claim(**args),
    "pair_release": lambda session, args: session.tool_pair_release(**args),
    "pair_send": lambda session, args: session.tool_pair_send(**args),
    "pair_read": lambda session, args: session.tool_pair_read(**args),
    "pair_advance": lambda session, args: session.tool_pair_advance(**args),
    "pair_patch_reserve": lambda session, args: session.tool_pair_patch_reserve(),
    "pair_patch_seal": lambda session, args: session.tool_pair_patch_seal(**args),
    "pair_patch_verify": lambda session, args: session.tool_pair_patch_verify(**args),
    "pair_patch_release": lambda session, args: session.tool_pair_patch_release(**args),
    "pair_note": lambda session, args: session.tool_pair_note(**args),
    "pair_transcript": lambda session, args: session.tool_pair_transcript(**args),
    "pair_unblock": lambda session, args: session.tool_pair_unblock(**args),
}


# ----------------------------------------------------------------- MCP server


class MCPServer:
    def __init__(self, session: PairSession) -> None:
        self.session = session
        self.initialized = False

    def send(self, payload: dict[str, Any]) -> None:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    def reply(self, request_id: Any, result: dict[str, Any]) -> None:
        self.send({"jsonrpc": "2.0", "id": request_id, "result": result})

    def error(self, request_id: Any, code: int, message: str, data: Any = None) -> None:
        error: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        self.send({"jsonrpc": "2.0", "id": request_id, "error": error})

    def run(self) -> None:
        log(f"ready role={self.session.role} state={self.session.store.root}")
        # readline() rather than iteration: iterating sys.stdin uses a read-ahead
        # buffer that withholds complete lines until the buffer fills, which on a
        # pipe deadlocks against a client waiting for our reply.
        while True:
            raw = sys.stdin.readline()
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
                    "Call pair_state first to see where you stand."
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
        result = self.session.invoke(name, arguments)
        text = json.dumps(result, ensure_ascii=False, indent=2)
        self.reply(request_id, {"content": [{"type": "text", "text": text}], "isError": False})

    # Declared capabilities are tools only, but some clients probe the rest.
    def on_resources_list(self, request_id: Any, params: dict[str, Any]) -> None:
        self.reply(request_id, {"resources": []})

    def on_resources_templates_list(self, request_id: Any, params: dict[str, Any]) -> None:
        self.reply(request_id, {"resourceTemplates": []})

    def on_prompts_list(self, request_id: Any, params: dict[str, Any]) -> None:
        self.reply(request_id, {"prompts": []})

    def on_logging_setLevel(self, request_id: Any, params: dict[str, Any]) -> None:
        self.reply(request_id, {})


# ------------------------------------------------------------------ pairctl


def run_ctl(args: argparse.Namespace, state_dir: Path) -> int:
    store = Store(state_dir)
    command = args.ctl_command

    if command == "status":
        state = store.read()
        print(json.dumps(
            {
                "state_dir": str(store.root),
                "task": state.get("task", ""),
                "mode": state.get("mode", "fix"),
                "writer": state.get("writer"),
                "policy": state.get("policy"),
                "phase": state.get("phase"),
                "round": state.get("round"),
                "roles": {name: info.get("last_seen") for name, info in state.get("roles", {}).items()},
            },
            indent=2,
            ensure_ascii=False,
        ))
        return 0

    if command == "assign":
        role = args.role
        known = set(store.read().get("roles", {}))
        if known and role not in known:
            print(f"warning: {role!r} has never connected; registered roles: {', '.join(sorted(known))}")
        def apply(state):
            state["writer"] = role
            # решение человека — повод поставить метку «выбрано человеком», чтобы
            # в переписке было видно, что это не гонка
            state["writer_reason"] = "назначено человеком"
            if state.get("closed_at") or state.get("phase") == "done":
                state.pop("closed_at", None)
                state["phase"] = "implement"
                state["round"] = int(state.get("round", 0)) + 1
            elif state.get("phase") == "scoping" and not state.get("task"):
                state["round"] = int(state.get("round", 0)) + 1
        store.mutate(apply)
        store.append_transcript("assign", "human", writer=role,
                                reason="назначено человеком")
        print(f"write authority assigned to {role}")
        return 0

    if command == "roles":
        # Постоянная расстановка: кто всегда пишет, кто всегда ревьюит.
        # Гонка остаётся как запасной вариант, но по умолчанию роли известны
        # заранее, и раунд начинается с работы, а не с торговли правами.
        policy_path = global_roles_path()
        state = store.read()
        current = state.get("policy") if isinstance(state.get("policy"), dict) else {}
        wanted = dict(current)

        if args.clear:
            wanted = {}
        else:
            if args.writer:
                wanted["writer"] = args.writer
            if args.critic:
                wanted["critic"] = args.critic
            if args.swap:
                was_writer, was_critic = wanted.get("writer"), wanted.get("critic")
                if was_writer or was_critic:
                    wanted["writer"] = was_critic
                    wanted["critic"] = was_writer

        if not wanted:
            store.mutate(lambda st: st.pop("policy", None))
            store.append_transcript("roles", "human", policy=None)
            print("постоянная расстановка снята, роли снова определяются гонкой")
            return 0

        if wanted.get("writer") == wanted.get("critic"):
            print("error: писатель и критик не могут быть одной ролью", file=sys.stderr)
            return 2

        store.mutate(lambda st: st.__setitem__("policy", wanted))
        store.append_transcript("roles", "human", policy=wanted, source=str(policy_path))

        # Расстановка сразу применяется к текущему раунду: назначенный писатель
        # получает право, иначе право остаётся у прошлого держателя и работа
        # стоит до вмешательства человека.
        holder = state.get("writer")
        if wanted.get("writer") and holder != wanted["writer"]:
            def apply(st):
                st["writer"] = wanted["writer"]
                st["writer_reason"] = "назначено человеком постоянной расстановкой"
            store.mutate(apply)

        if args.global_:
            policy_path.parent.mkdir(parents=True, exist_ok=True)
            policy_path.write_text(json.dumps(wanted, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"сохранено для любых проектов: {policy_path}")
        print(f"расстановка: писатель {wanted.get('writer') or '—'}, критик {wanted.get('critic') or '—'}")
        return 0

    if command == "release":
        def apply(state):
            state["writer"] = None
            state["writer_reason"] = ""
        store.mutate(apply)
        store.append_transcript("release", "human")
        print("write authority cleared; nobody holds it")
        return 0

    if command == "mode":
        mode = args.mode
        if mode not in MODES:
            print(f"error: unknown mode {mode!r}; expected one of {', '.join(MODES)}", file=sys.stderr)
            return 2
        previous = store.read().get("mode", "fix")
        store.mutate(lambda state: state.__setitem__("mode", mode))
        store.append_transcript("mode", "human", mode=mode, previous=previous)
        print(f"round mode is now {mode!r}")
        return 0

    if command == "why":
        # Почему сейчас пишет тот, кто пишет. Короткий вопрос, на который агент
        # обязан уметь ответить фактом, а не «ща посмотрю».
        rows = [project_row(root) for root in discover_projects(args.paths)]
        if args.json:
            print(json.dumps([{"project": row["project"], "writer": row["writer"],
                               "reason": row.get("reason", "")} for row in rows],
                             ensure_ascii=False, indent=2))
            return 0
        if not rows:
            print("ни одного проекта с состоянием пары не найдено")
            return 1
        for row in rows:
            print(f"{row['project']}: {row['writer'] or '—'} — {row.get('reason') or 'причина не сказана'}")
        return 0

    if command == "task":
        def apply(state):
            state["task"] = args.text
        store.mutate(apply)
        store.append_transcript("task", "human", text=args.text)
        print("task recorded")
        return 0

    if command == "inbox":
        role = args.role
        inbox = store.mailbox_root / role
        if not inbox.is_dir():
            print(f"no inbox for {role!r} (they may not have started yet)")
            return 0
        files = sorted(inbox.glob("*.json"))
        print(f"{len(files)} message(s) waiting for {role}")
        for path in files:
            body = json.loads(path.read_text(encoding="utf-8"))
            print(f"\n--- #{body['seq']} {body['ts']} from {body['from']} ---")
            print(body["text"])
        return 0

    if command == "log":
        for entry in store.read_transcript(args.limit):
            print(json.dumps(entry, ensure_ascii=False))
        return 0

    if command == "all":
        # Deliberately ignores --state: "all" is exactly the command where one
        # state directory would defeat the point.
        rows = [project_row(root, args.stuck_min) for root in discover_projects(args.paths)]
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
            return 0
        if not rows:
            print("ни одного проекта с состоянием пары не найдено")
            print("укажите путь:  pairctl all ~/work/foo")
            print("или скажите где искать:  PAIR_SCAN='~/work/*/.pair'")
            return 1
        print(render_table(rows))
        stuck = [row for row in rows if row["stuck"]]
        live = [row for row in rows if not row["closed"]]
        untouched = [row for row in rows if row["silence_seconds"] is None]
        print()
        print(f"{len(rows)} проект(ов), в работе {len(live)}, не начато {len(untouched)}")
        if stuck:
            names = ", ".join(row["project"] for row in stuck)
            print(f"молчат дольше {args.stuck_min} мин: {names}")
            print("  посмотреть:  PAIR_STATE=<путь> bin/pairchat --once")
            return 3
        return 0

    if command == "notes":
        for path in sorted(store.notes_root.glob("*.md")):
            print(f"\n===== {path.name} =====")
            print(path.read_text(encoding="utf-8"))
        return 0

    if command == "reset":
        if not args.yes:
            print("refusing to reset without --yes", file=sys.stderr)
            return 2
        import shutil

        for path in (store.mailbox_root, store.notes_root, store.patches_root):
            shutil.rmtree(path, ignore_errors=True)
        store.state_path.unlink(missing_ok=True)
        store.transcript_path.unlink(missing_ok=True)
        Store(store.root)
        print("state reset")
        return 0

    print(f"unknown command {command!r}", file=sys.stderr)
    return 2


def extract_state(argv: list[str]) -> tuple[Path, list[str]]:
    """Pull --state out of argv wherever it appears and resolve it.

    Subparser defaults silently overwrite same-named top-level options in
    argparse, so the option is removed from the grammar entirely and handled
    here instead. Both `server.py --state X ctl status` and
    `server.py ctl status --state X` work.
    """
    raw = ".pair"
    rest: list[str] = []
    index = 0
    while index < len(argv):
        item = argv[index]
        if item == "--state" and index + 1 < len(argv):
            raw = argv[index + 1]
            index += 2
            continue
        if item.startswith("--state="):
            raw = item.split("=", 1)[1]
            index += 1
            continue
        rest.append(item)
        index += 1
    return Path(raw).expanduser().resolve(), rest


# Where discover_projects looks when the human did not say. PAIR_SCAN overrides it
# with a colon-separated list of globs, so "where should I look" is always something
# the human can answer rather than something we guess at.
DEFAULT_SCAN_GLOBS = ("~/work/*/.pair",)


def discover_projects(extra: list[str] | None = None) -> list[Path]:
    """Every project that actually has pair state, sorted by path.

    A directory only counts when state.json is really inside it. An empty or
    half-created .pair is not a project, and listing it would put a phantom row
    in the human's overview of what is running.

    Naming projects replaces the built-in search. Otherwise `pairchat --all
    ~/work/blog` would show the five projects the scan always finds plus blog,
    and the human who named one project would get six.
    """
    import glob as _glob

    raw = os.environ.get("PAIR_SCAN", "").strip()
    if raw:
        patterns = [p.strip() for p in raw.split(os.pathsep) if p.strip()]
    elif extra:
        patterns = []
    else:
        patterns = list(DEFAULT_SCAN_GLOBS)

    found: dict[str, Path] = {}

    def offer(candidate: Path) -> None:
        candidate = candidate.expanduser()
        # Accept either the .pair directory or the project that contains it, because
        # "pairctl all ~/work/test" is what a human will type.
        if candidate.is_dir() and not (candidate / "state.json").is_file():
            candidate = candidate / ".pair"
        if (candidate / "state.json").is_file():
            resolved = candidate.resolve()
            found[str(resolved)] = resolved

    for pattern in patterns:
        expanded = str(Path(pattern).expanduser()) if pattern.startswith("~") else pattern
        for hit in _glob.glob(expanded, recursive=True):
            offer(Path(hit))
    for given in extra or []:
        offer(Path(given))
    # Never lose the project the human is standing in, whatever PAIR_SCAN says.
    offer(Path.cwd() / ".pair")

    return [found[key] for key in sorted(found)]


def silence_seconds(root: Path) -> float | None:
    """Seconds since the last recorded event, or None when nothing was recorded.

    Taken from the transcript, not from state.json's updated_at: a state write that
    changed nothing would otherwise dress up a stalled pair as busy.
    """
    last: dict | None = None
    try:
        text = (root / "transcript.jsonl").read_text(errors="replace")
    except OSError:
        return None
    for raw in text.splitlines():
        if not raw.strip():
            continue
        try:
            last = json.loads(raw)
        except Exception:
            continue
    if not last or not last.get("ts"):
        return None
    try:
        stamp = datetime.fromisoformat(str(last["ts"]))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - stamp).total_seconds())


def human_age(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 90:
        return f"{int(seconds)} с"
    if seconds < 5400:
        return f"{int(seconds // 60)} мин"
    if seconds < 172800:
        return f"{int(seconds // 3600)} ч"
    return f"{int(seconds // 86400)} д"


def project_row(root: Path, stuck_min: int = 30) -> dict:
    """One project's row for the human's overview. Never raises on a broken file."""
    try:
        state = json.loads((root / "state.json").read_text(encoding="utf-8"))
    except Exception:
        state = {}
    roles = state.get("roles") or {}
    writer = state.get("writer")
    age = silence_seconds(root)
    phase = state.get("phase") or "?"
    closed = bool(state.get("closed_at")) or phase == "done"
    return {
        "path": str(root),
        "project": root.parent.name or str(root),
        "task": (state.get("task") or "").strip(),
        "phase": phase,
        "writer": writer,
        "reason": (state.get("writer_reason") or "").strip(),
        "mode": state.get("mode", "fix"),
        "peers": sorted(name for name in roles if name != writer),
        "roles": sorted(roles),
        "silence_seconds": age,
        "silence": human_age(age),
        "closed": closed,
        "stuck": age is not None and not closed and age > stuck_min * 60,
    }


def render_table(rows: list[dict]) -> str:
    """Aligned plain text. Column widths come from the data, never from a guess."""
    header = ("проект", "фаза", "режим", "писатель", "критик", "тишина", "почему он", "задача")
    table = [
        (
            row["project"],
            row["phase"],
            "AUDIT" if row.get("mode") == "audit" else "fix",
            row["writer"] or "—",
            ", ".join(row["peers"]) or "—",
            "не начат" if row["silence_seconds"] is None
            else row["silence"] + ("  ЗАВИС" if row["stuck"] else ""),
            (row["reason"] or "—")[:40],
            (row["task"] or "—")[:36],
        )
        for row in rows
    ]
    widths = [
        max([len(header[i])] + [len(record[i]) for record in table]) for i in range(len(header))
    ]
    lines = ["  ".join(header[i].ljust(widths[i]) for i in range(len(header))).rstrip()]
    lines.append("  ".join("-" * w for w in widths))
    lines.extend(
        "  ".join(record[i].ljust(widths[i]) for i in range(len(record))).rstrip()
        for record in table
    )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pair-mailbox",
        description="MCP server and control tool for a two-agent collaboration.",
    )
    parser.add_argument("--role", help="This agent's role name. Fixed at configuration time.")

    sub = parser.add_subparsers(dest="ctl_command")
    sub.add_parser("status", help="show shared state")

    assign = sub.add_parser("assign", help="human: give write authority to a role")
    assign.add_argument("role")

    sub.add_parser("release", help="human: clear write authority")

    roles = sub.add_parser(
        "roles",
        help="human: pin who always writes and who always reviews, instead of racing each round",
    )
    roles.add_argument("--writer", help="role that always holds write authority")
    roles.add_argument("--critic", help="role that always reviews")
    roles.add_argument("--swap", action="store_true", help="exchange writer and critic")
    roles.add_argument("--clear", action="store_true", help="drop the pinning, go back to the race")
    roles.add_argument("--global", dest="global_", action="store_true",
                       help="also remember it for every project (PAIR_ROLES, default ~/.config/pair-mailbox/roles.json)")

    round_mode = sub.add_parser(
        "mode",
        help="human: choose what a round is for — 'fix' changes code, 'audit' does not",
    )
    round_mode.add_argument("mode", choices=list(MODES))

    task = sub.add_parser("task", help="human: record the task in shared state")
    task.add_argument("text")

    inbox = sub.add_parser("inbox", help="show a role's pending messages")
    inbox.add_argument("role")

    log = sub.add_parser("log", help="print the audit trail")
    log.add_argument("--limit", type=int, default=0)

    sub.add_parser("notes", help="print shared notes")

    everywhere = sub.add_parser(
        "all", help="one row per project, across every project with a .pair"
    )
    everywhere.add_argument("paths", nargs="*", help="extra project or .pair directories")
    everywhere.add_argument("--stuck-min", type=int, default=30)
    everywhere.add_argument("--json", action="store_true")

    why = sub.add_parser("why", help="who writes, and the reason they gave")
    why.add_argument("paths", nargs="*", help="extra project or .pair directories")
    why.add_argument("--json", action="store_true")

    reset = sub.add_parser("reset", help="wipe shared state")
    reset.add_argument("--yes", action="store_true")

    return parser


def main(argv: list[str] | None = None) -> int:
    state_dir, rest = extract_state(list(sys.argv[1:] if argv is None else argv))
    args = build_parser().parse_args(rest)
    if args.ctl_command:
        return run_ctl(args, state_dir)
    if not args.role:
        print("error: --role is required when running as an MCP server", file=sys.stderr)
        return 2
    store = Store(state_dir)
    MCPServer(PairSession(args.role, store)).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())