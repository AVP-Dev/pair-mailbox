"""Thread-safe worker scheduling over the fenced SQLite task store.

Only tasks carrying execution routing metadata are eligible. Pair governance
tasks remain untouched, while worker jobs use TaskStore leases as the durable
authority for claims, heartbeats, retries, and stale-owner fencing.
"""
from __future__ import annotations

import copy
import math
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Mapping

from task_store import StaleTaskFence, TaskStore, TaskStoreError


class WorkerPoolError(RuntimeError):
    """A worker cannot be scheduled safely in the current pool state."""


class NoAdapterError(WorkerPoolError):
    """No registered adapter satisfies a task's execution requirements."""


class WorkerStatus(str, Enum):
    IDLE = "idle"
    BUSY = "busy"
    DRAINING = "draining"
    CRASHED = "crashed"


@dataclass(frozen=True)
class Adapter:
    cli_profile: str
    handler: Callable[[dict[str, Any]], Any]
    capabilities: frozenset[str]


class AdapterRegistry:
    """Resolve task metadata to one registered CLI adapter."""

    def __init__(self) -> None:
        self._adapters: dict[str, Adapter] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _names(values: Any, field: str) -> frozenset[str]:
        if values is None:
            return frozenset()
        if isinstance(values, str):
            values = (values,)
        if not isinstance(values, (list, tuple, set, frozenset)):
            raise WorkerPoolError(f"{field} must be a string or a collection of strings")
        names = frozenset(str(value).strip() for value in values)
        if any(not name for name in names):
            raise WorkerPoolError(f"{field} cannot contain empty values")
        return names

    def register(self, cli_profile: str, handler: Callable[[dict[str, Any]], Any],
                 capabilities: Any = ()) -> Adapter:
        profile = str(cli_profile).strip()
        if not profile:
            raise WorkerPoolError("cli_profile cannot be empty")
        if not callable(handler):
            raise WorkerPoolError("adapter handler must be callable")
        adapter = Adapter(profile, handler, self._names(capabilities, "capabilities"))
        with self._lock:
            if profile in self._adapters:
                raise WorkerPoolError(f"adapter for {profile!r} is already registered")
            self._adapters[profile] = adapter
        return adapter

    def get(self, cli_profile: str) -> Adapter:
        with self._lock:
            try:
                return self._adapters[str(cli_profile)]
            except KeyError as exc:
                raise NoAdapterError(f"no adapter registered for {cli_profile!r}") from exc

    @staticmethod
    def _task_requirements(task: Mapping[str, Any]) -> tuple[str | None, frozenset[str]]:
        requirements = task.get("execution_requirements", task.get("requirements", {}))
        if requirements is None:
            requirements = {}
        if not isinstance(requirements, Mapping):
            raise WorkerPoolError("execution_requirements must be an object")
        profile = task.get("cli_profile", requirements.get("cli_profile"))
        profile = str(profile).strip() if profile is not None else None
        if profile == "":
            raise WorkerPoolError("cli_profile cannot be empty")
        capabilities = requirements.get("capabilities", task.get("capabilities", ()))
        return profile, AdapterRegistry._names(capabilities, "execution capabilities")

    def select(self, task: Mapping[str, Any],
               cli_profile: str | None = None) -> Adapter:
        required_profile, required_capabilities = self._task_requirements(task)
        if required_profile is not None and cli_profile is not None and required_profile != cli_profile:
            raise NoAdapterError(
                f"task requires {required_profile!r}, worker uses {cli_profile!r}"
            )
        profile = required_profile or cli_profile
        with self._lock:
            candidates = ([self._adapters[profile]] if profile in self._adapters else []) if profile else [
                self._adapters[key] for key in sorted(self._adapters)
            ]
        for adapter in candidates:
            if required_capabilities <= adapter.capabilities:
                return adapter
        if profile:
            raise NoAdapterError(
                f"no adapter for {profile!r} provides {sorted(required_capabilities)}"
            )
        raise NoAdapterError(
            f"no adapter provides {sorted(required_capabilities)}"
        )


@dataclass
class _Worker:
    worker_id: str
    cli_profile: str
    capabilities: frozenset[str]
    last_heartbeat: float
    status: WorkerStatus = WorkerStatus.IDLE
    process: Any = None
    task: dict[str, Any] | None = None
    lease_token: int | None = None
    fencing_token: int | None = None
    lease_expires_at: float | None = None
    heartbeat_count: int = 0
    heartbeat_stop: threading.Event | None = None
    heartbeat_thread: threading.Thread | None = None
    heartbeat_interval: float | None = None
    heartbeat_error: str | None = None


@dataclass(frozen=True)
class ClaimedTask:
    task: dict[str, Any]
    adapter: Adapter
    lease: dict[str, Any]


class WorkerPool:
    """Schedule metadata-routed tasks using SQLite's transactional leases.

    A worker adapter performs the job outside the pool. It must heartbeat before
    the lease expires and treat a rejected heartbeat/completion as cancellation.
    """

    def __init__(self, task_store: TaskStore, adapters: AdapterRegistry, *,
                 lease_seconds: float = 60.0, heartbeat_timeout: float = 20.0,
                 preempt_grace_seconds: float = 5.0,
                 auto_heartbeat: bool = True,
                 clock: Callable[[], float] = time.time) -> None:
        for name, value in (("lease_seconds", lease_seconds),
                            ("heartbeat_timeout", heartbeat_timeout),
                            ("preempt_grace_seconds", preempt_grace_seconds)):
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be positive and finite")
        if heartbeat_timeout >= lease_seconds:
            raise ValueError("heartbeat_timeout must be shorter than lease_seconds")
        self.task_store = task_store
        self.adapters = adapters
        self.lease_seconds = float(lease_seconds)
        self.heartbeat_timeout = float(heartbeat_timeout)
        self.preempt_grace_seconds = float(preempt_grace_seconds)
        self.auto_heartbeat = bool(auto_heartbeat)
        self.clock = clock
        self._workers: dict[str, _Worker] = {}
        self._lock = threading.RLock()

    def submit_task(self, task: dict[str, Any], *, actor: str = "worker-pool",
                    idempotency_key: str | None = None) -> tuple[dict[str, Any], bool]:
        """Persist a runnable job; governance tasks without routing stay separate."""
        payload = dict(task)
        if not self._has_execution_metadata(payload):
            raise WorkerPoolError("worker task needs cli_profile or execution requirements")
        payload.setdefault("status", "open")
        if payload["status"] != "open":
            raise WorkerPoolError("new worker tasks must be open")
        payload.setdefault("execution_status", "ready")
        payload.setdefault("attempts", 0)
        if payload["execution_status"] != "ready":
            raise WorkerPoolError("new worker tasks must be ready")
        try:
            attempts = int(payload["attempts"])
            max_attempts = int(payload.get("max_attempts", 3))
        except (TypeError, ValueError) as exc:
            raise WorkerPoolError("attempts and max_attempts must be integers") from exc
        if attempts < 0 or max_attempts < 1 or attempts >= max_attempts:
            raise WorkerPoolError("worker task retry limits are invalid")
        self.adapters.select(payload)
        return self.task_store.add_task(payload, actor, idempotency_key)

    @staticmethod
    def _has_execution_metadata(task: Mapping[str, Any]) -> bool:
        return any(key in task for key in
                   ("cli_profile", "execution_requirements", "requirements"))

    def register_worker(self, worker_id: str, cli_profile: str, *,
                        capabilities: Any = None, process: Any = None,
                        heartbeat_interval: float | None = None,
                        now: float | None = None) -> dict[str, Any]:
        identity = str(worker_id).strip()
        profile = str(cli_profile).strip()
        if not identity:
            raise WorkerPoolError("worker_id cannot be empty")
        adapter = self.adapters.get(profile)
        declared = (adapter.capabilities if capabilities is None else
                    AdapterRegistry._names(capabilities, "worker capabilities"))
        if not declared <= adapter.capabilities:
            raise WorkerPoolError(
                f"worker capabilities exceed adapter {profile!r}: "
                f"{sorted(declared - adapter.capabilities)}"
            )
        timestamp = self._time(now)
        with self._lock:
            previous = self._workers.get(identity)
            if previous is not None and previous.status is not WorkerStatus.CRASHED:
                raise WorkerPoolError(f"worker {identity!r} is already registered")
            worker = _Worker(identity, profile, declared, timestamp, process=process)
            self._workers[identity] = worker
            view = self._worker_view(worker)
        if self.auto_heartbeat:
            self.start_heartbeats(identity, interval=heartbeat_interval)
        return view

    def worker(self, worker_id: str) -> dict[str, Any]:
        with self._lock:
            return self._worker_view(self._require_worker(worker_id))

    def workers(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self._worker_view(self._workers[key]) for key in sorted(self._workers)]

    @staticmethod
    def _worker_view(worker: _Worker) -> dict[str, Any]:
        return {
            "worker_id": worker.worker_id,
            "cli_profile": worker.cli_profile,
            "capabilities": sorted(worker.capabilities),
            "status": worker.status.value,
            "last_heartbeat": worker.last_heartbeat,
            "active_task_id": worker.task.get("id") if worker.task else None,
            "lease_expires_at": worker.lease_expires_at,
            "heartbeat_error": worker.heartbeat_error,
        }

    def claim_next(self, worker_id: str, *, now: float | None = None) -> ClaimedTask | None:
        timestamp = self._time(now)
        self.reap_stale_workers(now=timestamp)
        with self._lock:
            worker = self._require_worker(worker_id)
            if worker.status is not WorkerStatus.IDLE:
                raise WorkerPoolError(
                    f"worker {worker.worker_id!r} cannot claim while {worker.status.value}"
                )
            for task in self.task_store.list_tasks():
                if (task.get("status") != "open"
                        or task.get("execution_status", "ready") != "ready"
                        or not self._has_execution_metadata(task)):
                    continue
                try:
                    attempts = int(task.get("attempts", 0))
                    max_attempts = int(task.get("max_attempts", 3))
                    fence = int(task["fencing_token"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise WorkerPoolError(
                        f"task #{task.get('id')} has invalid worker retry metadata"
                    ) from exc
                if attempts < 0 or max_attempts < 1:
                    raise WorkerPoolError(f"task #{task.get('id')} has invalid retry limits")
                if attempts >= max_attempts:
                    try:
                        self.task_store.transition_task(
                            int(task["id"]),
                            {"execution_status": "failed", "worker_id": None,
                             "last_error": task.get("last_error") or
                             "maximum worker attempts exhausted"},
                            "worker-pool", "worker_exhausted", fence,
                            idempotency_key=f"worker-exhausted:{task['id']}:{fence}",
                            now=timestamp,
                        )
                    except StaleTaskFence:
                        continue
                    continue
                try:
                    adapter = self.adapters.select(task, worker.cli_profile)
                except NoAdapterError:
                    continue
                _, required_capabilities = self.adapters._task_requirements(task)
                if not required_capabilities <= worker.capabilities:
                    continue
                try:
                    lease, _ = self.task_store.acquire_lease(
                        int(task["id"]), worker.worker_id, self.lease_seconds,
                        idempotency_key=uuid.uuid4().hex, now=timestamp,
                    )
                except StaleTaskFence:
                    continue
                except TaskStoreError as exc:
                    # A competing worker may have won between list and acquire.
                    if "already has an active lease" in str(exc) or "is not open" in str(exc):
                        continue
                    raise
                attempts += 1
                claimed, _ = self.task_store.transition_task(
                    int(task["id"]),
                    {"execution_status": "busy", "worker_id": worker.worker_id,
                     "attempts": attempts},
                    worker.worker_id, "worker_claim", int(lease["fencing_token"]),
                    lease_token=int(lease["lease_token"]),
                    idempotency_key=f"worker-claim:{task['id']}:{lease['lease_token']}",
                    now=timestamp,
                )
                lease["fencing_token"] = int(claimed["fencing_token"])
                lease["version"] = int(claimed["version"])
                worker.status = WorkerStatus.BUSY
                worker.task = claimed
                worker.lease_token = int(lease["lease_token"])
                worker.fencing_token = int(lease["fencing_token"])
                worker.lease_expires_at = float(lease["expires_at"])
                worker.last_heartbeat = timestamp
                worker.heartbeat_count = 0
                return ClaimedTask(copy.deepcopy(claimed), adapter, dict(lease))
            return None

    def start_heartbeats(self, worker_id: str, *, interval: float | None = None) -> None:
        """Start a daemon that renews this worker's lease at a bounded interval."""
        delay = (min(self.lease_seconds, self.heartbeat_timeout) / 3
                 if interval is None else float(interval))
        if (not math.isfinite(delay) or delay <= 0
                or delay >= min(self.lease_seconds, self.heartbeat_timeout)):
            raise ValueError("heartbeat interval must be shorter than lease and worker timeouts")
        with self._lock:
            worker = self._require_worker(worker_id)
            if worker.status in (WorkerStatus.DRAINING, WorkerStatus.CRASHED):
                raise WorkerPoolError(
                    f"worker {worker.worker_id!r} cannot heartbeat while {worker.status.value}"
                )
            if worker.heartbeat_thread is not None and worker.heartbeat_thread.is_alive():
                return
            stop = threading.Event()
            thread = threading.Thread(
                target=self._heartbeat_loop, args=(worker.worker_id, stop, delay),
                name=f"pair-worker-heartbeat-{worker.worker_id}", daemon=True,
            )
            worker.heartbeat_stop = stop
            worker.heartbeat_thread = thread
            worker.heartbeat_interval = delay
            worker.heartbeat_error = None
            thread.start()

    def stop_heartbeats(self, worker_id: str, *, timeout: float | None = None) -> bool:
        with self._lock:
            worker = self._require_worker(worker_id)
            stop = worker.heartbeat_stop
            thread = worker.heartbeat_thread
            interval = worker.heartbeat_interval or 0.0
            if stop is not None:
                stop.set()
        if thread is None or thread is threading.current_thread():
            return True
        thread.join(timeout=max(0.1, interval * 2) if timeout is None else timeout)
        return not thread.is_alive()

    def _heartbeat_loop(self, worker_id: str, stop: threading.Event,
                        interval: float) -> None:
        while not stop.wait(interval):
            try:
                self.heartbeat(worker_id, _stop_event=stop)
            except Exception as exc:
                if stop.is_set():
                    return
                with self._lock:
                    worker = self._workers.get(worker_id)
                    if worker is not None:
                        worker.heartbeat_error = str(exc)
                # A worker whose lease cannot be renewed must stop before the
                # expired lease can be reclaimed by another process.
                try:
                    self.preempt_worker(worker_id)
                except Exception as stop_error:
                    with self._lock:
                        worker = self._workers.get(worker_id)
                        if worker is not None:
                            worker.heartbeat_error = (
                                f"{worker.heartbeat_error}; stop failed: {stop_error}"
                            )
                return

    def heartbeat(self, worker_id: str, *, now: float | None = None,
                  _stop_event: threading.Event | None = None) -> dict[str, Any]:
        timestamp = self._time(now)
        with self._lock:
            if _stop_event is not None and _stop_event.is_set():
                raise WorkerPoolError("worker heartbeat loop is stopped")
            worker = self._require_worker(worker_id)
            if worker.status is WorkerStatus.CRASHED:
                raise WorkerPoolError(f"worker {worker.worker_id!r} is crashed")
            if worker.task is not None:
                assert worker.lease_token is not None
                assert worker.fencing_token is not None
                sequence = worker.heartbeat_count + 1
                lease, _ = self.task_store.renew_lease(
                    int(worker.task["id"]), worker.worker_id, worker.lease_token,
                    self.lease_seconds,
                    idempotency_key=(f"worker-heartbeat:{worker.worker_id}:"
                                     f"{worker.task['id']}:{sequence}"),
                    now=timestamp,
                )
                worker.lease_token = int(lease["lease_token"])
                worker.fencing_token = int(lease["fencing_token"])
                worker.lease_expires_at = float(lease["expires_at"])
                worker.task["fencing_token"] = worker.fencing_token
                worker.task["version"] = int(lease["version"])
                worker.heartbeat_count = sequence
            worker.last_heartbeat = timestamp
            return self._worker_view(worker)

    def complete_task(self, worker_id: str, *, result: str = "",
                      now: float | None = None) -> dict[str, Any]:
        timestamp = self._time(now)
        with self._lock:
            worker = self._require_running_worker(worker_id)
            task = self._active_task(worker)
            updates = {
                "status": "done",
                "execution_status": "done",
                "worker_id": None,
                "result": str(result),
                "closed_at": datetime.fromtimestamp(timestamp, timezone.utc).isoformat(
                    timespec="seconds"),
                "closed_by": worker.worker_id,
            }
            saved, _ = self.task_store.transition_task(
                int(task["id"]), updates, worker.worker_id, "worker_complete",
                int(worker.fencing_token), lease_token=int(worker.lease_token),
                idempotency_key=f"worker-complete:{task['id']}:{worker.lease_token}",
                now=timestamp,
            )
            self._clear_task(worker, WorkerStatus.IDLE)
            worker.last_heartbeat = timestamp
            return saved

    def fail_task(self, worker_id: str, error: str, *, retry: bool | None = None,
                  now: float | None = None) -> dict[str, Any]:
        timestamp = self._time(now)
        with self._lock:
            worker = self._require_running_worker(worker_id)
            saved = self._fail_active_task(worker, str(error), retry, timestamp,
                                           after_preemption=False)
            return saved

    def preempt_worker(self, worker_id: str, *, now: float | None = None) -> dict[str, Any]:
        timestamp = self._time(now)
        with self._lock:
            worker = self._require_worker(worker_id)
            if worker.status is WorkerStatus.CRASHED:
                return self._worker_view(worker)
            self._stop_heartbeat(worker)
            worker.status = WorkerStatus.DRAINING
            self._stop_process(worker.process)
            worker.process = None
            if worker.task is not None:
                self._fail_active_task(worker, "worker preempted", True, timestamp,
                                       after_preemption=True)
            else:
                worker.status = WorkerStatus.DRAINING
            return self._worker_view(worker)

    def reap_stale_workers(self, *, now: float | None = None) -> list[str]:
        timestamp = self._time(now)
        stale: list[str] = []
        with self._lock:
            for worker in self._workers.values():
                if (worker.status is WorkerStatus.CRASHED
                        or (worker.status is WorkerStatus.DRAINING and worker.process is None)):
                    continue
                if timestamp - worker.last_heartbeat < self.heartbeat_timeout:
                    continue
                # A failed termination leaves the worker draining and the lease
                # intact, so another worker cannot overlap a possibly live job.
                worker.status = WorkerStatus.DRAINING
                self._stop_heartbeat(worker)
                self._stop_process(worker.process)
                worker.process = None
                worker.status = WorkerStatus.CRASHED
                stale.append(worker.worker_id)
            reclaimed = self.task_store.reclaim_expired_leases(now=timestamp)
            reclaimed_set = set(reclaimed)
            for worker in self._workers.values():
                if worker.task is not None and worker.task.get("id") in reclaimed_set:
                    self._clear_task(worker, WorkerStatus.CRASHED)
        return stale

    def _fail_active_task(self, worker: _Worker, error: str, retry: bool | None,
                          now: float, *, after_preemption: bool) -> dict[str, Any]:
        task = self._active_task(worker)
        attempt = int(task.get("attempts", 1))
        max_attempts = int(task.get("max_attempts", 3))
        if max_attempts < 1:
            raise WorkerPoolError("max_attempts must be positive")
        should_retry = attempt < max_attempts if retry is None else bool(retry)
        state = "ready" if should_retry else "failed"
        saved, _ = self.task_store.transition_task(
            int(task["id"]),
            {"execution_status": state, "worker_id": None,
             "last_error": error[:2048]},
            worker.worker_id, "worker_retry" if should_retry else "worker_failed",
            int(worker.fencing_token), lease_token=int(worker.lease_token),
            idempotency_key=f"worker-failure:{task['id']}:{worker.lease_token}",
            now=now, release_lease=True,
        )
        self._clear_task(worker, WorkerStatus.DRAINING if after_preemption else WorkerStatus.IDLE)
        worker.last_heartbeat = now
        return saved

    @staticmethod
    def _active_task(worker: _Worker) -> dict[str, Any]:
        if (worker.status not in (WorkerStatus.BUSY, WorkerStatus.DRAINING)
                or worker.task is None
                or worker.lease_token is None or worker.fencing_token is None):
            raise WorkerPoolError(f"worker {worker.worker_id!r} has no active task")
        return worker.task

    @staticmethod
    def _clear_task(worker: _Worker, status: WorkerStatus) -> None:
        worker.task = None
        worker.lease_token = None
        worker.fencing_token = None
        worker.lease_expires_at = None
        worker.heartbeat_count = 0
        worker.status = status

    @staticmethod
    def _stop_heartbeat(worker: _Worker) -> None:
        if worker.heartbeat_stop is not None:
            worker.heartbeat_stop.set()

    def _require_running_worker(self, worker_id: str) -> _Worker:
        worker = self._require_worker(worker_id)
        if worker.status is not WorkerStatus.BUSY:
            raise WorkerPoolError(f"worker {worker.worker_id!r} is not busy")
        return worker

    def _require_worker(self, worker_id: str) -> _Worker:
        try:
            return self._workers[str(worker_id)]
        except KeyError as exc:
            raise WorkerPoolError(f"worker {worker_id!r} is not registered") from exc

    def _stop_process(self, process: Any) -> None:
        if process is None:
            return
        poll = getattr(process, "poll", None)
        if callable(poll) and poll() is not None:
            return
        terminate = getattr(process, "terminate", None)
        wait = getattr(process, "wait", None)
        kill = getattr(process, "kill", None)
        if not callable(terminate) or not callable(wait) or not callable(kill):
            raise WorkerPoolError("worker process must expose poll/terminate/wait/kill")
        terminate()  # subprocess.Popen.terminate sends SIGTERM on POSIX.
        try:
            wait(timeout=self.preempt_grace_seconds)
        except subprocess.TimeoutExpired:
            kill()  # subprocess.Popen.kill sends SIGKILL on POSIX.
            wait()

    def _time(self, value: float | None) -> float:
        timestamp = self.clock() if value is None else float(value)
        if not math.isfinite(timestamp):
            raise ValueError("time must be finite")
        return timestamp
