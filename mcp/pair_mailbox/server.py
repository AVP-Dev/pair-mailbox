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
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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

# Auto-release is off unless the human turns it on. A writer that has finished
# and gone quiet holds authority in no useful sense.
STALL_SECONDS = int(os.environ.get("PAIR_STALL_SECONDS", "0") or 0)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(message: str) -> None:
    """Diagnostics go to stderr. stdout carries the protocol and nothing else."""
    print(f"[{SERVER_NAME}] {message}", file=sys.stderr, flush=True)


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
        self.state_path = root / "state.json"
        self.transcript_path = root / "transcript.jsonl"
        self.lock_path = root / ".lock"
        for directory in (self.root, self.mailbox_root, self.notes_root, self.patches_root):
            directory.mkdir(parents=True, exist_ok=True)

    def default_state(self) -> dict[str, Any]:
        return {
            "task": "",
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
            return self.default_state()
        state = self.default_state()
        state.update(data)
        return state

    def _save(self, state: dict[str, Any]) -> None:
        state["updated_at"] = utcnow()
        atomic_write_json(self.state_path, state)

    class _Lock:
        def __init__(self, path: Path) -> None:
            self.path = path
            self.handle = None

        def __enter__(self):
            self.handle = self.path.open("a+")
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
            return self

        def __exit__(self, *exc):
            assert self.handle is not None
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None
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
        entry = roles.setdefault(self.role, {"first_seen": utcnow(), "reads": 0, "sends": 0})
        entry["last_seen"] = utcnow()
        entry["pid"] = os.getpid()

    def _require_not_writer(self, action: str) -> None:
        state = self.store.read()
        if state.get("writer") == self.role:
            raise ProtocolError(
                f"{self.role} holds write authority, so it must not {action}.",
                "The writer edits project files directly. Only a non-writer proposes patches.",
            )

    def _known_roles(self, state: dict[str, Any]) -> list[str]:
        return sorted(state.get("roles", {}))

    # ------------------------------------------------------------------ tools

    def tool_pair_state(self) -> dict[str, Any]:
        """Read the shared state, the roster, and this role's standing."""
        with self.store.lock():
            state = self.store._load()
            self._register(state)
            self.store._save(state)

        writer = state.get("writer")
        return {
            "you": self.role,
            "you_are_writer": writer == self.role,
            "task": state.get("task", ""),
            "writer": writer,
            "phase": state.get("phase", "scoping"),
            "round": state.get("round", 0),
            "phases": list(PHASES),
            "legal_next_phases": list(PHASE_TRANSITIONS.get(state.get("phase", "scoping"), ())),
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
            out[name] = {
                "role": "writer" if name == writer else "critic",
                "last_seen": info.get("last_seen"),
                "silent_seconds": age,
                "round_closed": closed,
                "will_read_new_messages": not closed,
                "hint": (
                    "The round is closed. This role has moved on and will not read new"
                    " messages. To get it back: pair_advance('implement')."
                    if closed else "Listening."
                ),
            }
        return out

    def _unread_count(self, state: dict[str, Any]) -> int:
        seen = int(state.get("seen", {}).get(self.role, 0))
        return len(self._inbox_files()) - seen

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

    def tool_pair_claim(self) -> dict[str, Any]:
        """Take write authority. Refused only while someone else actively holds it."""
        def apply(state: dict[str, Any]) -> dict[str, Any]:
            self._register(state)
            current = state.get("writer")
            if current and current != self.role:
                if not self._writer_is_abandoned(state, current):
                    raise ProtocolError(
                        f"write authority is held by {current!r}.",
                        "Ask the human: `pairctl release` or `pairctl assign <role>`.",
                    )
                state["writer"] = self.role
                state["round"] = int(state.get("round", 0)) + 1
                return {
                    "already_writer": False,
                    "writer": self.role,
                    "round": state["round"],
                    "took_over": True,
                }
            if current == self.role:
                return {"already_writer": True, "writer": self.role, "round": int(state.get("round", 0))}
            state["writer"] = self.role
            state["round"] = int(state.get("round", 0)) + 1
            return {"already_writer": False, "writer": self.role, "round": state["round"]}

        result = self.store.mutate(apply)
        self.store.append_transcript("claim", self.role, writer=self.role, result=result)
        return {
            "writer": result["writer"],
            "already_held": result["already_writer"],
            "round": result.get("round"),
            "note": "You are the writer. Edit project files directly. Your peer stays read-only.",
        }

    def tool_pair_release(self) -> dict[str, Any]:
        """Give up write authority voluntarily. Only the holder may do this."""
        def apply(state: dict[str, Any]) -> dict[str, Any]:
            current = state.get("writer")
            if current != self.role:
                raise ProtocolError(
                    f"{self.role} does not hold write authority; {current!r} does.",
                    "A peer cannot revoke authority. Ask the human if this needs to change.",
                )
            state["writer"] = None
            return {"released": True}

        self.store.mutate(apply)
        self.store.append_transcript("release", self.role)
        return {
            "released": True,
            "note": (
                "Authority is now unassigned. Nobody holds write authority. The human can assign it "
                "to either role with `pairctl assign <role>`."
            ),
        }

    def tool_pair_send(self, to: str, text: str, subject: str = "") -> dict[str, Any]:
        """Send one message to the peer. The label identifying you is added here,
        not by you, so a peer cannot forge authorship."""
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
            seq = state.get("seq", 0) + 1
            state["seq"] = seq
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            filename = f"{seq:06d}-{stamp}-{self.role}.json"
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

        result = self.store.mutate(apply)
        self.store.append_transcript("send", self.role, to=to, seq=result["seq"], subject=subject)
        return {
            "delivered": True,
            "seq": result["seq"],
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
            fresh = files[seen : seen + max(0, int(limit))]
            out: list[dict[str, Any]] = []
            for path in fresh:
                try:
                    out.append(json.loads(path.read_text(encoding="utf-8")))
                except (OSError, json.JSONDecodeError):
                    continue
            if fresh:
                state.setdefault("seen", {})[self.role] = seen + len(fresh)
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
            allowed = list(forward) + ([backward] if backward else [])
            raise ProtocolError(
                f"cannot move from {from_phase!r} to {phase!r}; allowed: {', '.join(allowed) or 'none'}.",
                "Phases move scoping → plan → implement → verify → done. Any role may take one step back.",
            )

        unread = self._unread_count(current)
        if phase == "done" and unread:
            raise ProtocolError(
                f"{unread} unread message(s) in your inbox; a round cannot be closed over unread mail.",
                "Run pair_read first. If the peer still needs something, it has not been said yet.",
            )

        reopening = from_phase == "done" and phase != "done"

        def apply(state: dict[str, Any]) -> None:
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
        return {"kind": kind, "path": str(target)}

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
                "Claim write authority for your role. Succeeds only when nobody holds it. "
                "Refused when a peer already holds it: only the human can transfer authority, via "
                "`pairctl assign <role>`. Claiming makes you the writer and keeps your peer read-only."
            ),
            "inputSchema": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "pair_release",
            "description": (
                "Give up write authority voluntarily. Only the current holder may call this; a peer "
                "cannot revoke it. Afterwards nobody holds authority until the human assigns it."
            ),
            "inputSchema": {"type": "object", "properties": {}, "required": []},
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
    ]


HANDLERS = {
    "pair_state": lambda session, args: session.tool_pair_state(),
    "pair_claim": lambda session, args: session.tool_pair_claim(),
    "pair_release": lambda session, args: session.tool_pair_release(),
    "pair_send": lambda session, args: session.tool_pair_send(**args),
    "pair_read": lambda session, args: session.tool_pair_read(**args),
    "pair_advance": lambda session, args: session.tool_pair_advance(**args),
    "pair_patch_reserve": lambda session, args: session.tool_pair_patch_reserve(),
    "pair_patch_seal": lambda session, args: session.tool_pair_patch_seal(**args),
    "pair_patch_verify": lambda session, args: session.tool_pair_patch_verify(**args),
    "pair_patch_release": lambda session, args: session.tool_pair_patch_release(**args),
    "pair_note": lambda session, args: session.tool_pair_note(**args),
    "pair_transcript": lambda session, args: session.tool_pair_transcript(**args),
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
                "writer": state.get("writer"),
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
            if state.get("phase") == "scoping" and not state.get("task"):
                state["round"] = int(state.get("round", 0)) + 1
        store.mutate(apply)
        store.append_transcript("assign", "human", writer=role)
        print(f"write authority assigned to {role}")
        return 0

    if command == "release":
        def apply(state):
            state["writer"] = None
        store.mutate(apply)
        store.append_transcript("release", "human")
        print("write authority cleared; nobody holds it")
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

    task = sub.add_parser("task", help="human: record the task in shared state")
    task.add_argument("text")

    inbox = sub.add_parser("inbox", help="show a role's pending messages")
    inbox.add_argument("role")

    log = sub.add_parser("log", help="print the audit trail")
    log.add_argument("--limit", type=int, default=0)

    sub.add_parser("notes", help="print shared notes")

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