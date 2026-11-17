"""MCP bridge for the durable execution queue and coordinator.

Governance tasks remain PairSession's concern. This bridge exposes execution jobs
only through explicit metadata and returns claims to the calling CLI; it does not
shell out or invent an execution adapter.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any

from coordinator import CoordinatorError, MultiAgentCoordinator
from task_store import TaskStore
from worker_pool import AdapterRegistry, WorkerPool, WorkerPoolError


class ExecutionAPI:
    def __init__(self, task_store: TaskStore, role: str) -> None:
        self.task_store = task_store
        self.role = role
        self.adapters = AdapterRegistry()
        profiles_path = Path(__file__).resolve().parents[2] / "cli-profiles.json"
        try:
            profiles = json.loads(profiles_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            # Installed MCP copies can live outside the project checkout. Keep
            # the shipped six profiles available there; local project additions
            # are loaded from cli-profiles.json when present.
            profiles = {name: {} for name in
                        ("opencode", "agy", "codex", "claude", "pi", "omp")}
        except json.JSONDecodeError as exc:
            raise WorkerPoolError(f"CLI profiles are invalid JSON: {exc}") from exc
        self.profiles = frozenset(key for key, value in profiles.items()
                                  if not key.startswith("_") and isinstance(value, dict))
        # These registry entries are routing descriptors only. The claimed task is
        # returned to the bound CLI, which executes it and reports completion.
        for profile in sorted(self.profiles):
            self.adapters.register(profile, lambda task: task)
        lease_seconds = self._positive_setting("PAIR_WORKER_LEASE_SECONDS", 60.0)
        heartbeat_timeout = self._positive_setting(
            "PAIR_WORKER_HEARTBEAT_TIMEOUT_SECONDS", min(20.0, lease_seconds / 3)
        )
        if heartbeat_timeout >= lease_seconds:
            raise WorkerPoolError(
                "PAIR_WORKER_HEARTBEAT_TIMEOUT_SECONDS must be shorter than the lease"
            )
        self.pool = WorkerPool(
            task_store, self.adapters, lease_seconds=lease_seconds,
            heartbeat_timeout=heartbeat_timeout, auto_heartbeat=False,
        )
        self.coordinator = MultiAgentCoordinator(task_store)

    @staticmethod
    def _positive_setting(name: str, default: float) -> float:
        raw = os.environ.get(name)
        if raw is None or not raw.strip():
            return default
        try:
            value = float(raw)
        except ValueError as exc:
            raise WorkerPoolError(f"{name} must be a positive number") from exc
        if not math.isfinite(value) or value <= 0:
            raise WorkerPoolError(f"{name} must be a positive finite number")
        return value

    def submit(self, task: dict[str, Any], *, actor: str,
               idempotency_key: str | None = None) -> tuple[dict[str, Any], bool]:
        try:
            return self.pool.submit_task(task, actor=actor,
                                         idempotency_key=idempotency_key)
        except WorkerPoolError as exc:
            raise CoordinatorError(str(exc)) from exc

    def worker_call(self, action: str, *, worker_id: str = "", cli_profile: str = "",
                    capabilities: Any = None, result: str = "", error: str = "",
                    retry: bool | None = None) -> dict[str, Any]:
        action = str(action).strip()
        identity = str(worker_id or self.role).strip()
        if identity != self.role:
            raise CoordinatorError("a role may operate only its own execution worker")
        try:
            if action == "register":
                if cli_profile not in self.profiles:
                    raise CoordinatorError(f"unknown CLI profile {cli_profile!r}")
                try:
                    return {"worker": self.pool.worker(identity), "existing": True}
                except WorkerPoolError:
                    pass
                return {"worker": self.pool.register_worker(
                    identity, cli_profile, capabilities=capabilities
                ), "existing": False}
            if action == "claim":
                claimed = self.pool.claim_next(identity)
                if claimed is None:
                    return {"claimed": None}
                # The CLI reports explicit heartbeats while it works. No adapter
                # callback or background heartbeat runs inside the MCP server.
                return {"claimed": {"task": claimed.task, "lease": claimed.lease,
                                    "cli_profile": claimed.adapter.cli_profile}}
            if action == "heartbeat":
                return {"worker": self.pool.heartbeat(identity)}
            if action == "complete":
                return {"task": self.pool.complete_task(identity, result=result)}
            if action == "fail":
                return {"task": self.pool.fail_task(identity, error=error, retry=retry)}
            if action == "status":
                return {"worker": self.pool.worker(identity)}
        except (WorkerPoolError, ValueError) as exc:
            raise CoordinatorError(str(exc)) from exc
        raise CoordinatorError("action must be register, claim, heartbeat, complete, fail or status")

    def coordinator_call(self, action: str, *, peer_id: str = "", worker_id: str = "",
                          members: Any = None, proposal_id: str = "",
                          approve: bool | None = None) -> dict[str, Any]:
        action = str(action).strip()
        try:
            if action == "register_peer":
                peer = str(peer_id or self.role).strip()
                worker = str(worker_id or self.role).strip()
                if peer != self.role or worker != self.role:
                    raise CoordinatorError("a session may register only its bound role as a peer")
                return {"peer": self.coordinator.register_peer(
                    peer, self.pool, worker
                )}
            if action == "status":
                return {"peers": self.coordinator.peers(),
                        "topology": self.coordinator.topology()}
            if action == "propose":
                return {"proposal": self.coordinator.propose_topology(
                    self.role, members or []
                )}
            if action == "vote":
                if not isinstance(approve, bool):
                    raise CoordinatorError("approve must be a boolean")
                return {"proposal": self.coordinator.vote(
                    proposal_id, self.role, approve
                )}
            if action == "dispatch":
                dispatch = self.coordinator.dispatch_next()
                return {"dispatch": None if dispatch is None else {
                    "peer_id": dispatch.peer_id, "worker_id": dispatch.worker_id,
                    "task": dispatch.claimed.task, "lease": dispatch.claimed.lease,
                    "cli_profile": dispatch.claimed.adapter.cli_profile,
                }}
        except (WorkerPoolError, CoordinatorError, ValueError) as exc:
            raise CoordinatorError(str(exc)) from exc
        raise CoordinatorError("action must be register_peer, status, propose, vote or dispatch")
