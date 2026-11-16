"""Durable task registry used by :mod:`store_core`.

Task rows move from ``state.json`` to ``state.db`` once. A preparing marker plus
an exact backup makes an interrupted first migration recoverable; a complete
marker makes the database authoritative and forbids JSON fallback.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

TASK_STATUSES = ("open", "done", "dropped")
SCHEMA_VERSION = "1"
BUSY_TIMEOUT_MS = 5000


class TaskStoreError(RuntimeError):
    """The task database cannot safely serve or accept task state."""


class StaleTaskFence(TaskStoreError):
    """A task transition used an expired or superseded fencing token."""


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write(path: Path, body: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-task-")
    temp_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
        _sync_directory(path.parent)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def _legacy_tasks(raw: bytes) -> list[dict[str, Any]]:
    try:
        state = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TaskStoreError(f"legacy state.json is unreadable; task migration stopped: {exc}") from exc
    if not isinstance(state, dict):
        raise TaskStoreError("legacy state.json is not an object; task migration stopped")
    if "tasks" in state:
        tasks = state.get("tasks")
        if not isinstance(tasks, list):
            raise TaskStoreError("legacy state.json tasks field is not a list")
        return [_validate_task(item) for item in tasks]
    task_text = str(state.get("task") or "").strip()
    if not task_text:
        return []
    return [_validate_task({
        "id": 1,
        "text": task_text,
        "status": "open",
        "added_at": state.get("created_at"),
        "added_by": "human",
        "round": int(state.get("round", 0)),
        "reason": "",
        "closed_at": None,
        "closed_by": None,
    })]


def _validate_task(task: Any) -> dict[str, Any]:
    if not isinstance(task, dict):
        raise TaskStoreError("task rows must be JSON objects")
    result = dict(task)
    try:
        task_id = int(result["id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise TaskStoreError("task row has no valid integer id") from exc
    if task_id < 1:
        raise TaskStoreError("task ids must be positive")
    result["id"] = task_id
    status = str(result.get("status") or "open")
    if status not in TASK_STATUSES:
        raise TaskStoreError(f"task #{task_id} has unknown status {status!r}")
    result["status"] = status
    return result


def _stored_task(task: dict[str, Any]) -> dict[str, Any]:
    """Strip DB concurrency metadata from the compatibility task payload."""
    result = _validate_task(task)
    result.pop("version", None)
    result.pop("fencing_token", None)
    return result


def _public_task(task: dict[str, Any], version: int, fencing_token: int) -> dict[str, Any]:
    result = _validate_task(task)
    result["version"] = int(version)
    result["fencing_token"] = int(fencing_token)
    return result


class TaskStore:
    """SQLite WAL task persistence with one-way migration and fenced transitions."""

    def __init__(self, root: Path, state_path: Path) -> None:
        self.root = Path(root)
        self.state_path = Path(state_path)
        self.db_path = self.root / "state.db"
        self.marker_path = self.root / "state.db.cutover.json"
        self.backup_path = self.root / "state.json.pre-state-db.bak"
        self._initialize()

    def _read_marker(self) -> dict[str, Any] | None:
        if not self.marker_path.exists():
            return None
        try:
            value = json.loads(self.marker_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TaskStoreError(f"task DB cutover marker is unreadable: {exc}") from exc
        if not isinstance(value, dict) or value.get("status") not in ("preparing", "complete"):
            raise TaskStoreError("task DB cutover marker is invalid; refusing JSON fallback")
        return value

    def _write_marker(self, marker: dict[str, Any]) -> None:
        _atomic_write(self.marker_path, (_canonical(marker) + "\n").encode("utf-8"))

    def _backup_legacy(self, raw: bytes) -> None:
        if self.backup_path.exists():
            try:
                previous = self.backup_path.read_bytes()
            except OSError as exc:
                raise TaskStoreError(f"legacy state backup is unreadable: {exc}") from exc
            if previous != raw:
                raise TaskStoreError("existing legacy state backup differs; migration stopped")
            return
        _atomic_write(self.backup_path, raw)

    def _initialize(self) -> None:
        marker = self._read_marker()
        if marker is not None:
            if marker["status"] == "complete":
                self._require_authoritative(marker)
                return
            self._recover_preparing(marker)
            return

        if self.db_path.exists():
            raise TaskStoreError(
                "state.db exists without a cutover marker; refusing stale state.json fallback"
            )
        try:
            raw = self.state_path.read_bytes() if self.state_path.exists() else b"{}"
        except OSError as exc:
            raise TaskStoreError(f"legacy state.json is unreadable; task migration stopped: {exc}") from exc
        source_hash = hashlib.sha256(raw).hexdigest()
        self._backup_legacy(raw)
        tasks = _legacy_tasks(raw)
        preparing = {
            "status": "preparing", "schema_version": SCHEMA_VERSION,
            "source_sha256": source_hash, "task_count": len(tasks),
            "source_exists": self.state_path.exists(),
        }
        self._write_marker(preparing)
        self._create_database(tasks, source_hash)
        self._write_marker({**preparing, "status": "complete"})

    def _recover_preparing(self, marker: dict[str, Any]) -> None:
        source_hash = marker.get("source_sha256")
        if not isinstance(source_hash, str) or len(source_hash) != 64:
            raise TaskStoreError("preparing marker has no valid source hash")
        if self.db_path.exists() and self._database_is_complete(source_hash):
            self._write_marker({**marker, "status": "complete"})
            return
        try:
            raw = self.backup_path.read_bytes()
        except OSError as exc:
            raise TaskStoreError(f"pre-cutover backup is unavailable: {exc}") from exc
        if hashlib.sha256(raw).hexdigest() != source_hash:
            raise TaskStoreError("pre-cutover backup hash mismatch; migration stopped")
        tasks = _legacy_tasks(raw)
        if self.db_path.exists():
            self._quarantine_incomplete_database()
        self._create_database(tasks, source_hash)
        self._write_marker({**marker, "status": "complete"})

    def _quarantine_incomplete_database(self) -> None:
        suffix = f".incomplete-{uuid.uuid4().hex}"
        for source in (self.db_path, Path(str(self.db_path) + "-wal"),
                       Path(str(self.db_path) + "-shm")):
            if source.exists():
                os.replace(source, Path(str(source) + suffix))
        _sync_directory(self.root)

    def _connect(self, *, create: bool = False, check: bool = True) -> sqlite3.Connection:
        if not create and not self.db_path.is_file():
            raise TaskStoreError("authoritative state.db is missing; refusing JSON fallback")
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                str(self.db_path), timeout=BUSY_TIMEOUT_MS / 1000,
                isolation_level=None,
            )
            connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA foreign_keys=ON")
            pragma = "PRAGMA journal_mode=WAL" if create else "PRAGMA journal_mode"
            mode = str(connection.execute(pragma).fetchone()[0]).lower()
            if mode != "wal":
                raise TaskStoreError(f"state.db journal mode is {mode!r}, expected WAL")
            self._secure_files()
            if check:
                self._integrity_check(connection)
                meta = dict(connection.execute("SELECT key, value FROM task_meta"))
                if (meta.get("cutover_complete") != "1"
                        or meta.get("schema_version") != SCHEMA_VERSION):
                    raise TaskStoreError("state.db has no valid completed cutover marker")
            return connection
        except TaskStoreError:
            if connection is not None:
                connection.close()
            raise
        except sqlite3.Error as exc:
            if connection is not None:
                connection.close()
            raise TaskStoreError(f"authoritative state.db is unreadable; refusing JSON fallback: {exc}") from exc
        except BaseException:
            if connection is not None:
                connection.close()
            raise

    def _secure_files(self) -> None:
        for path in (self.db_path, Path(str(self.db_path) + "-wal"),
                     Path(str(self.db_path) + "-shm")):
            if path.exists():
                os.chmod(path, 0o600)

    @staticmethod
    def _integrity_check(connection: sqlite3.Connection) -> None:
        try:
            result = connection.execute("PRAGMA quick_check").fetchone()
        except sqlite3.Error as exc:
            raise TaskStoreError(f"state.db integrity check failed: {exc}") from exc
        if not result or result[0] != "ok":
            raise TaskStoreError(f"state.db integrity check failed: {result[0] if result else 'no result'}")

    def _database_is_complete(self, source_hash: str) -> bool:
        try:
            connection = self._connect(check=True)
            try:
                self._integrity_check(connection)
                meta = dict(connection.execute("SELECT key, value FROM task_meta"))
                return (meta.get("cutover_complete") == "1"
                        and meta.get("source_sha256") == source_hash
                        and meta.get("schema_version") == SCHEMA_VERSION)
            finally:
                connection.close()
        except (TaskStoreError, sqlite3.Error):
            return False

    def _require_authoritative(self, marker: dict[str, Any]) -> None:
        if marker.get("schema_version") != SCHEMA_VERSION:
            raise TaskStoreError("unsupported task DB schema; refusing JSON fallback")
        if not self._database_is_complete(str(marker.get("source_sha256") or "")):
            raise TaskStoreError("authoritative state.db is incomplete or corrupt; task operations frozen")

    def _create_database(self, tasks: list[dict[str, Any]], source_hash: str) -> None:
        connection = self._connect(create=True, check=False)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TABLE task_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE tasks ("
                "id INTEGER PRIMARY KEY, payload_json TEXT NOT NULL, version INTEGER NOT NULL, "
                "fencing_token INTEGER NOT NULL, lease_token INTEGER NOT NULL DEFAULT 0, "
                "lease_owner TEXT, lease_expires_at REAL)"
            )
            connection.execute(
                "CREATE TABLE task_transitions ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER NOT NULL, "
                "actor TEXT NOT NULL, idempotency_key TEXT NOT NULL, action TEXT NOT NULL, "
                "fencing_token INTEGER NOT NULL, at REAL NOT NULL, request_json TEXT NOT NULL, "
                "result_json TEXT NOT NULL, "
                "UNIQUE(actor, idempotency_key))"
            )
            for task in tasks:
                payload = _canonical(task)
                connection.execute(
                    "INSERT INTO tasks(id, payload_json, version, fencing_token) VALUES (?, ?, 0, 0)",
                    (task["id"], payload),
                )
                connection.execute(
                    "INSERT INTO task_transitions(task_id, actor, idempotency_key, action, "
                    "fencing_token, at, request_json, result_json) "
                    "VALUES (?, 'migration', ?, 'migrate', 0, ?, ?, ?)",
                    (task["id"], f"migration:{source_hash}:{task['id']}", time.time(),
                     _canonical({"source_sha256": source_hash, "task_id": task["id"]}), payload),
                )
            for key, value in (("schema_version", SCHEMA_VERSION),
                               ("cutover_complete", "1"),
                               ("source_sha256", source_hash)):
                connection.execute("INSERT INTO task_meta(key, value) VALUES (?, ?)", (key, value))
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            connection.close()
            raise
        connection.close()
        _sync_directory(self.root)

    def list_tasks(self) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT payload_json, version, fencing_token FROM tasks ORDER BY id"
            ).fetchall()
            try:
                tasks = [_public_task(json.loads(row[0]), int(row[1]), int(row[2]))
                         for row in rows]
            except (TypeError, json.JSONDecodeError) as exc:
                raise TaskStoreError(f"state.db task payload is corrupt: {exc}") from exc
            return [_validate_task(task) for task in tasks]
        except sqlite3.Error as exc:
            raise TaskStoreError(f"authoritative state.db task read failed: {exc}") from exc
        finally:
            connection.close()

    def _idempotent_result(self, connection: sqlite3.Connection, actor: str,
                           key: str | None,
                           request: dict[str, Any] | None = None) -> dict[str, Any] | None:
        if not key:
            return None
        row = connection.execute(
            "SELECT request_json, result_json FROM task_transitions "
            "WHERE actor=? AND idempotency_key=?",
            (actor, key),
        ).fetchone()
        if row is None:
            return None
        if request is not None and row[0] != _canonical(request):
            raise TaskStoreError("idempotency key was already used for a different request")
        try:
            return json.loads(row[1])
        except (TypeError, json.JSONDecodeError) as exc:
            raise TaskStoreError(f"idempotency result is corrupt: {exc}") from exc

    @staticmethod
    def _record(connection: sqlite3.Connection, task_id: int, actor: str, key: str,
                action: str, fence: int, result: dict[str, Any],
                request: dict[str, Any] | None = None) -> None:
        connection.execute(
            "INSERT INTO task_transitions(task_id, actor, idempotency_key, action, "
            "fencing_token, at, request_json, result_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, actor, key, action, fence, time.time(), _canonical(request or {}),
             _canonical(result)),
        )

    def add_task(self, task: dict[str, Any], actor: str,
                 idempotency_key: str | None = None) -> tuple[dict[str, Any], bool]:
        request = {"action": "add", "text": str(task.get("text") or ""),
                   "round": int(task.get("round", 0))}
        item = dict(task)
        key = str(idempotency_key or uuid.uuid4().hex)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            replay = self._idempotent_result(connection, actor, idempotency_key, request)
            if replay is not None:
                connection.commit()
                return _validate_task(replay), True
            if item.get("id") is None:
                item["id"] = int(connection.execute(
                    "SELECT COALESCE(MAX(id), 0) + 1 FROM tasks").fetchone()[0])
            item = _stored_task(item)
            if connection.execute("SELECT 1 FROM tasks WHERE id=?", (item["id"],)).fetchone():
                raise TaskStoreError(f"task #{item['id']} already exists")
            payload = _canonical(item)
            fence = 1
            connection.execute(
                "INSERT INTO tasks(id, payload_json, version, fencing_token) VALUES (?, ?, 1, ?)",
                (item["id"], payload, fence),
            )
            result = _public_task(item, 1, fence)
            self._record(connection, item["id"], actor, key, "add", fence,
                         result, request)
            connection.commit()
            return result, False
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def close_task(self, task_id: int, status: str, reason: str, actor: str,
                   expected_fencing_token: int | None = None,
                   idempotency_key: str | None = None,
                   round_number: int | None = None) -> tuple[dict[str, Any], bool]:
        if status not in ("done", "dropped"):
            raise TaskStoreError(f"invalid closed task status {status!r}")
        request = {"action": status, "task_id": int(task_id), "reason": str(reason or "").strip(),
                   "expected_fencing_token": expected_fencing_token}
        key = str(idempotency_key or uuid.uuid4().hex)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            replay = self._idempotent_result(connection, actor, idempotency_key, request)
            if replay is not None:
                connection.commit()
                return _validate_task(replay), True
            row = connection.execute(
                "SELECT payload_json, version, fencing_token FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            if row is None:
                known = ", ".join(str(item[0]) for item in connection.execute(
                    "SELECT id FROM tasks ORDER BY id")) or "none yet"
                raise TaskStoreError(f"no task #{task_id}. Known ids: {known}.")
            item = _validate_task(json.loads(row[0]))
            version, fence = int(row[1]), int(row[2])
            if expected_fencing_token is not None and expected_fencing_token != fence:
                raise StaleTaskFence(
                    f"task #{task_id} fencing token is stale (expected {fence}, got {expected_fencing_token})"
                )
            if item.get("status") != "open":
                raise TaskStoreError(f"task #{task_id} is already {item.get('status')!r}.")
            if status == "dropped" and not str(reason or "").strip():
                raise TaskStoreError(f"task #{task_id} cannot be dropped without a reason.")
            item.update({
                "status": status, "reason": str(reason or "").strip(),
                "closed_at": _utcnow(), "closed_by": actor,
            })
            if round_number is not None:
                item["closed_round"] = int(round_number)
            fence += 1
            changed = connection.execute(
                "UPDATE tasks SET payload_json=?, version=?, fencing_token=?, "
                "lease_owner=NULL, lease_expires_at=NULL "
                "WHERE id=? AND fencing_token=?",
                (_canonical(item), version + 1, fence, task_id, fence - 1),
            ).rowcount
            if changed != 1:
                raise StaleTaskFence(f"task #{task_id} changed during transition")
            result = _public_task(item, version + 1, fence)
            self._record(connection, task_id, actor, key, status, fence, result, request)
            connection.commit()
            return result, False
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def acquire_lease(self, task_id: int, owner: str, lease_seconds: float,
                      idempotency_key: str | None = None,
                      now: float | None = None) -> tuple[dict[str, Any], bool]:
        """Acquire a new monotonic lease epoch; expired epochs can never renew."""
        if not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise TaskStoreError("lease_seconds must be positive")
        actor = str(owner)
        key = str(idempotency_key or uuid.uuid4().hex)
        current_time = time.time() if now is None else float(now)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            request = {"action": "lease_acquire", "task_id": int(task_id),
                       "lease_seconds": float(lease_seconds)}
            replay = self._idempotent_result(connection, actor, idempotency_key, request)
            if replay is not None:
                connection.commit()
                return replay, True
            row = connection.execute(
                "SELECT payload_json, version, fencing_token, lease_token, lease_expires_at "
                "FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            if row is None:
                raise TaskStoreError(f"no task #{task_id}")
            item = _validate_task(json.loads(row[0]))
            version, fence, lease_token = map(int, row[1:4])
            expires_at = row[4]
            if item.get("status") != "open":
                raise TaskStoreError(f"task #{task_id} is not open")
            if expires_at is not None and float(expires_at) > current_time:
                raise TaskStoreError(f"task #{task_id} already has an active lease")
            lease_token += 1
            fence += 1
            lease = {"task_id": task_id, "owner": actor, "lease_token": lease_token,
                     "fencing_token": fence, "version": version + 1,
                     "expires_at": current_time + lease_seconds}
            changed = connection.execute(
                "UPDATE tasks SET version=?, fencing_token=?, lease_token=?, lease_owner=?, "
                "lease_expires_at=? WHERE id=? AND fencing_token=?",
                (version + 1, fence, lease_token, actor, lease["expires_at"], task_id, fence - 1),
            ).rowcount
            if changed != 1:
                raise StaleTaskFence(f"task #{task_id} lease changed during acquisition")
            self._record(connection, task_id, actor, key, "lease_acquired", fence, lease, request)
            connection.commit()
            return lease, False
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def renew_lease(self, task_id: int, owner: str, lease_token: int,
                    lease_seconds: float, idempotency_key: str | None = None,
                    now: float | None = None) -> tuple[dict[str, Any], bool]:
        """Renew only a live lease with its current epoch and fence."""
        if not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise TaskStoreError("lease_seconds must be positive")
        actor = str(owner)
        key = str(idempotency_key or uuid.uuid4().hex)
        current_time = time.time() if now is None else float(now)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            request = {"action": "lease_renew", "task_id": int(task_id),
                       "lease_token": int(lease_token), "lease_seconds": float(lease_seconds)}
            replay = self._idempotent_result(connection, actor, idempotency_key, request)
            if replay is not None:
                connection.commit()
                return replay, True
            row = connection.execute(
                "SELECT payload_json, version, fencing_token, lease_token, lease_owner, lease_expires_at "
                "FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            if row is None:
                raise TaskStoreError(f"no task #{task_id}")
            item = _validate_task(json.loads(row[0]))
            version, fence, current_token = map(int, row[1:4])
            if (item.get("status") != "open" or current_token != int(lease_token)
                    or row[4] != actor or row[5] is None or float(row[5]) <= current_time):
                raise StaleTaskFence(f"task #{task_id} lease is expired or fenced")
            fence += 1
            lease = {"task_id": task_id, "owner": actor, "lease_token": current_token,
                     "fencing_token": fence, "version": version + 1,
                     "expires_at": current_time + lease_seconds}
            changed = connection.execute(
                "UPDATE tasks SET version=?, fencing_token=?, lease_expires_at=? "
                "WHERE id=? AND fencing_token=? AND lease_token=? AND lease_expires_at>?",
                (version + 1, fence, lease["expires_at"], task_id, fence - 1,
                 lease_token, current_time),
            ).rowcount
            if changed != 1:
                raise StaleTaskFence(f"task #{task_id} lease changed during renewal")
            self._record(connection, task_id, actor, key, "lease_renewed", fence, lease, request)
            connection.commit()
            return lease, False
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def transition_task(self, task_id: int, updates: dict[str, Any], actor: str,
                        action: str, expected_fencing_token: int,
                        lease_token: int | None = None,
                        idempotency_key: str | None = None,
                        now: float | None = None) -> tuple[dict[str, Any], bool]:
        """Apply a fenced transition; supplying a lease token requires a live lease."""
        key = str(idempotency_key or uuid.uuid4().hex)
        current_time = time.time() if now is None else float(now)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            request = {"action": action, "task_id": int(task_id), "updates": updates,
                       "expected_fencing_token": int(expected_fencing_token),
                       "lease_token": lease_token}
            replay = self._idempotent_result(connection, actor, idempotency_key, request)
            if replay is not None:
                connection.commit()
                return _validate_task(replay), True
            row = connection.execute(
                "SELECT payload_json, version, fencing_token, lease_token, lease_owner, lease_expires_at "
                "FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            if row is None:
                raise TaskStoreError(f"no task #{task_id}")
            item = _validate_task(json.loads(row[0]))
            version, fence, current_lease = map(int, row[1:4])
            if fence != int(expected_fencing_token):
                raise StaleTaskFence(f"task #{task_id} fencing token is stale (current {fence})")
            if lease_token is not None and item.get("status") != "open":
                raise StaleTaskFence(f"task #{task_id} is closed")
            if lease_token is not None and (
                current_lease != int(lease_token) or row[4] != actor or row[5] is None
                or float(row[5]) <= current_time
            ):
                raise StaleTaskFence(f"task #{task_id} lease is expired or fenced")
            item.update(updates)
            item = _validate_task(item)
            fence += 1
            lease_update = (", lease_owner=NULL, lease_expires_at=NULL"
                            if item.get("status") in ("done", "dropped") else "")
            changed = connection.execute(
                "UPDATE tasks SET payload_json=?, version=?, fencing_token=?" + lease_update +
                " WHERE id=? AND fencing_token=?",
                (_canonical(item), version + 1, fence, task_id, fence - 1),
            ).rowcount
            if changed != 1:
                raise StaleTaskFence(f"task #{task_id} changed during transition")
            result = _public_task(item, version + 1, fence)
            self._record(connection, task_id, actor, key, action, fence, result, request)
            connection.commit()
            return result, False
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def sync_tasks(self, desired: list[dict[str, Any]], actor: str = "store") -> None:
        """Persist legacy Store.mutate task edits without losing direct callers."""
        items = [_stored_task(item) for item in desired]
        by_id = {item["id"]: item for item in items}
        if len(by_id) != len(items):
            raise TaskStoreError("duplicate task ids in state mutation")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT id, payload_json, version, fencing_token FROM tasks ORDER BY id"
            ).fetchall()
            old = {int(row[0]): (json.loads(row[1]), int(row[2]), int(row[3])) for row in rows}
            removed = sorted(set(old) - set(by_id))
            if removed:
                raise TaskStoreError(f"task history cannot be deleted (ids: {removed})")
            for task_id, item in by_id.items():
                previous = old.get(task_id)
                payload = _canonical(item)
                item_actor = str(item.get("closed_by") or item.get("added_by") or actor)
                key = uuid.uuid4().hex
                if previous is None:
                    connection.execute(
                        "INSERT INTO tasks(id, payload_json, version, fencing_token) VALUES (?, ?, 1, 1)",
                        (task_id, payload),
                    )
                    self._record(connection, task_id, item_actor, key, "legacy_add", 1, item,
                                 {"task": item})
                    continue
                old_payload, version, fence = previous
                if _canonical(old_payload) == payload:
                    continue
                new_fence = fence + 1
                changed = connection.execute(
                    "UPDATE tasks SET payload_json=?, version=?, fencing_token=?"
                    + (", lease_owner=NULL, lease_expires_at=NULL"
                       if item.get("status") in ("done", "dropped") else "")
                    + " WHERE id=? AND fencing_token=?",
                    (payload, version + 1, new_fence, task_id, fence),
                ).rowcount
                if changed != 1:
                    raise StaleTaskFence(f"task #{task_id} changed during legacy Store mutation")
                self._record(connection, task_id, item_actor, key, "legacy_update", new_fence,
                             item, {"task": item})
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def transition_count(self) -> int:
        connection = self._connect()
        try:
            return int(connection.execute("SELECT count(*) FROM task_transitions").fetchone()[0])
        except sqlite3.Error as exc:
            raise TaskStoreError(f"task transition read failed: {exc}") from exc
        finally:
            connection.close()


def _utcnow() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
