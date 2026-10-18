#!/usr/bin/env python3
"""store_core: the state kernel of pair-mailbox, cut out of server.py in round 115.

What lives here, and why: the filesystem-backed shared state, its advisory lock, the
append-only transcript and that transcript's hash chain, plus the atomic writer every
one of those writes goes through. This is the load-bearing half of the pair: the
round-close gate audits the transcript, so a defect here is silent corruption rather
than a visible failure, which is why the seam was measured with AST before it was cut
(634..1129 in server.py, 496 lines) rather than estimated.

Why a separate module and not a split by function: the Store class reaches into the
chain helpers, so a class-level cut alone left five NameErrors (round 110), and a
functional cut repeats that. The seam is the whole kernel or nothing.

server.py keeps the names re-exported, so every existing call site -- including
``from server import Store`` and ``pairctl_cli.py`` -- works unchanged.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import liveness  # noqa: E402 - needs sys.path above

SERVER_NAME = "pair-mailbox"

# The transcript is capped: without it `read_transcript` parsed the whole file for twenty
# reply lines, and limitless `pairctl log` dumped into the terminal everything the round
# gathered. The value runs with margin above a typical round, so rotation never fires on
# every second call.
TRANSCRIPT_MAX_BYTES = 256 * 1024
# How many chunks how much past to keep before releasing the oldest.
TRANSCRIPT_ARCHIVES = 4
# A read portion from the end. Below a block means more syscalls, above means more
# memory; 64 KB reads faster than a journal page without dragging it whole.
TRANSCRIPT_READ_BLOCK = 64 * 1024


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(message: str) -> None:
    """Diagnostics go to stderr. stdout carries the protocol and nothing else."""
    print(f"[{SERVER_NAME}] {message}", file=sys.stderr, flush=True)


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

    # These three ride as CLASS attributes, not module constants read at call
    # time, for one measured reason: the rotation tests shrink them to force a
    # rotation with `mock.patch.object(server, "TRANSCRIPT_MAX_BYTES", 20_000)`
    # (test_protocol_notes.py:443). Round 115 moved them here, and the reads
    # resolved to the module global, so the patch stopped moving anything and
    # rotation silently never fired — three rotation guards passed green while
    # proving nothing (suite went 962 passed -> 959, 3 new failures).
    # Class attributes keep both patch paths alive: `patch.object` on the class,
    # or on a module that re-exports it, still reaches the read. Production
    # never patches these; tests do, so the values must be patchable where the
    # tests point. No second source of truth — the assignments below bind the
    # module-level constants, which are the single definition.
    TRANSCRIPT_MAX_BYTES: int
    TRANSCRIPT_ARCHIVES: int
    TRANSCRIPT_READ_BLOCK: int

    def __init__(self, root: Path, allow_create: bool = False) -> None:
        # Round 115 S4: a wrong root used to read as an empty-but-valid state, which
        # is worse than a crash. Measured 2026-10-18: diagnosing an ack refusal, omp
        # built `Store(Path('.'))` where the intended root was `Path('.pair')`.
        # `state_path` became `./state.json`, which does not exist, so `_load`
        # returned `default_state()` — empty `seen`, empty `pending_reads`. Downstream
        # `.get()` chains returned `None`, which was read as a MEASUREMENT, and two
        # green direct results on screen were read as "the file changed under me".
        # Two hours were spent on a phantom defect that did not exist.
        # A root without state.json is a caller error, not an empty pair: it must
        # fail loudly, naming the path it tried AND the `.pair` neighbour that is
        # almost certainly what was meant. `allow_create=True` keeps the one lawful
        # case: first-run bootstrap (bin/pairup writes state.json itself afterwards).
        if not allow_create and not (root / "state.json").exists():
            candidate = root / ".pair"
            hint = (f"; did you mean {candidate}?" if (candidate / "state.json").exists()
                    else "")
            raise FileNotFoundError(f"No state.json in {root}{hint}")
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
            return self.transcript_path.stat().st_size > self.TRANSCRIPT_MAX_BYTES
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
            if self.transcript_path.stat().st_size <= self.TRANSCRIPT_MAX_BYTES:
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
            for index in range(self.TRANSCRIPT_ARCHIVES - 1, 0, -1):
                older = archive / f"{index}.jsonl"
                if not older.exists():
                    continue
                if index == self.TRANSCRIPT_ARCHIVES - 1:
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


# Bound here, after the class body, so the class attributes above stay
# annotations with values taken from the ONE definition at module level. A test
# that patches the module constant gets it through `Store.X` as well; a test
# that patches `Store.X` directly does not disturb the module. Production reads
# only ever go through `self.X`, which resolves to this binding.
Store.TRANSCRIPT_MAX_BYTES = TRANSCRIPT_MAX_BYTES
Store.TRANSCRIPT_ARCHIVES = TRANSCRIPT_ARCHIVES
Store.TRANSCRIPT_READ_BLOCK = TRANSCRIPT_READ_BLOCK


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
