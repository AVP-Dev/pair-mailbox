"""Coordinate peer topology consensus and lease-backed task dispatch.

The coordinator owns only its in-process peer registry and topology votes.
Task durability, retries, and worker ownership remain in TaskStore and
WorkerPool, which must point at the same SQLite database.
"""
from __future__ import annotations

import copy
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from task_store import TaskStore
from worker_pool import (
    ClaimedTask,
    NoAdapterError,
    WorkerPool,
    WorkerPoolError,
    WorkerStatus,
)


class CoordinatorError(RuntimeError):
    """A peer or topology operation is invalid or unsafe."""


@dataclass(frozen=True)
class TaskSubmission:
    peer_id: str
    task: dict[str, Any]
    replayed: bool


@dataclass(frozen=True)
class Dispatch:
    peer_id: str
    worker_id: str
    claimed: ClaimedTask


@dataclass(frozen=True)
class _Peer:
    peer_id: str
    worker_id: str
    pool: WorkerPool


@dataclass
class _Proposal:
    proposal_id: str
    base_revision: int
    membership_epoch: int
    members: tuple[str, ...]
    voters: tuple[str, ...]
    votes: dict[str, bool] = field(default_factory=dict)
    status: str = "pending"


class MultiAgentCoordinator:
    """Reach majority agreement on dispatch order, then delegate to WorkerPool.

    Each topology vote is scoped to the registered-peer snapshot captured when
    its proposal is made. Membership or topology changes stale outstanding
    proposals. This is a coordinator-local consensus protocol, not a network
    consensus or Byzantine fault-tolerance implementation.
    """

    def __init__(self, task_store: TaskStore) -> None:
        self.task_store = task_store
        self._store_path = Path(task_store.db_path).resolve()
        self._peers: dict[str, _Peer] = {}
        saved = task_store.get_runtime_state("coordinator", {})
        if not isinstance(saved, dict):
            raise CoordinatorError("persisted coordinator state is not an object")
        self._peer_descriptors: dict[str, str] = {
            str(key): str(value) for key, value in
            (saved.get("peers") if isinstance(saved.get("peers"), dict) else {}).items()
        }
        self._proposals: dict[str, _Proposal] = {}
        for row in saved.get("proposals", []):
            if not isinstance(row, dict) or not isinstance(row.get("proposal_id"), str):
                raise CoordinatorError("persisted coordinator proposal is invalid")
            proposal = _Proposal(
                proposal_id=row["proposal_id"],
                base_revision=int(row["base_revision"]),
                membership_epoch=int(row["membership_epoch"]),
                members=tuple(row["members"]), voters=tuple(row["voters"]),
                votes=dict(row.get("votes", {})), status=str(row.get("status", "pending")),
            )
            self._proposals[proposal.proposal_id] = proposal
        self._membership_epoch = int(saved.get("membership_epoch", 0))
        self._topology_revision = int(saved.get("topology_revision", 0))
        self._topology = tuple(saved.get("topology", ()))
        self._revoked_members = set(saved.get("revoked_members", ()))
        self._dispatch_cursor = int(saved.get("dispatch_cursor", 0))
        self._lock = threading.RLock()

    def register_peer(self, peer_id: str, pool: WorkerPool,
                      worker_id: str) -> dict[str, Any]:
        identity = self._identity(peer_id, "peer_id")
        worker = self._identity(worker_id, "worker_id")
        if not isinstance(pool, WorkerPool):
            raise CoordinatorError("peer pool must be a WorkerPool")
        if Path(pool.task_store.db_path).resolve() != self._store_path:
            raise CoordinatorError("all peers must share the coordinator TaskStore database")
        try:
            worker_view = pool.worker(worker)
        except WorkerPoolError as exc:
            raise CoordinatorError(f"worker {worker!r} is not registered in its pool") from exc
        if worker_view["status"] not in (WorkerStatus.IDLE.value, WorkerStatus.BUSY.value):
            raise CoordinatorError(
                f"worker {worker!r} cannot join while {worker_view['status']}"
            )

        with self._lock:
            self._refresh()
            if identity in self._peers:
                raise CoordinatorError(f"peer {identity!r} is already registered")
            if any(peer.worker_id == worker for peer in self._peers.values()):
                raise CoordinatorError(f"worker {worker!r} is already registered as a peer")
            self._peers[identity] = _Peer(identity, worker, pool)
            if identity not in self._peer_descriptors:
                self._membership_changed()
            self._peer_descriptors[identity] = worker
            self._persist()
            return self._peer_view(self._peers[identity])

    def unregister_peer(self, peer_id: str) -> bool:
        identity = self._identity(peer_id, "peer_id")
        with self._lock:
            self._refresh()
            peer = self._require_peer(identity)
            try:
                worker = peer.pool.worker(peer.worker_id)
            except WorkerPoolError:
                worker = {"active_task_id": None}
            if worker.get("active_task_id") is not None:
                raise CoordinatorError(
                    f"peer {identity!r} cannot leave while its worker has an active task"
                )
            if identity in self._topology:
                self._revoked_members.add(identity)
            del self._peers[identity]
            self._peer_descriptors.pop(identity, None)
            self._membership_changed()
            self._persist()
            return True

    def peers(self) -> list[dict[str, Any]]:
        with self._lock:
            self._refresh()
            return [self._peer_view(self._peers[key]) for key in sorted(self._peers)]

    def topology(self) -> dict[str, Any]:
        with self._lock:
            self._refresh()
            return {"revision": self._topology_revision,
                    "membership_epoch": self._membership_epoch,
                    "peers": list(self._topology),
                    "inactive_peers": sorted(self._revoked_members & set(self._topology))}

    def propose_topology(self, proposer_id: str,
                         members: Sequence[str]) -> dict[str, Any]:
        proposer = self._identity(proposer_id, "proposer_id")
        if isinstance(members, (str, bytes)) or not isinstance(members, (list, tuple)):
            raise CoordinatorError("topology members must be an ordered list of peer IDs")
        ordered = tuple(self._identity(member, "topology peer") for member in members)
        if not ordered:
            raise CoordinatorError("topology must contain at least one peer")
        if len(set(ordered)) != len(ordered):
            raise CoordinatorError("topology cannot repeat peer IDs")

        with self._lock:
            self._refresh()
            self._require_peer(proposer)
            unknown = [member for member in ordered if member not in self._peer_descriptors]
            if unknown:
                raise CoordinatorError(f"topology names unregistered peers: {unknown}")
            proposal = _Proposal(
                proposal_id=uuid.uuid4().hex,
                base_revision=self._topology_revision,
                membership_epoch=self._membership_epoch,
                members=ordered,
                voters=tuple(sorted(self._peer_descriptors)),
            )
            self._proposals[proposal.proposal_id] = proposal
            self._persist()
            return self._proposal_view(proposal)

    def vote(self, proposal_id: str, peer_id: str,
             approve: bool) -> dict[str, Any]:
        proposal_key = self._identity(proposal_id, "proposal_id")
        voter = self._identity(peer_id, "peer_id")
        if not isinstance(approve, bool):
            raise CoordinatorError("vote must be a boolean")
        with self._lock:
            self._refresh()
            def record_vote(saved: Any) -> dict[str, Any]:
                if not isinstance(saved, dict):
                    raise CoordinatorError("coordinator state is missing")
                proposals = saved.get("proposals", [])
                proposal = next((item for item in proposals
                                 if item.get("proposal_id") == proposal_key), None)
                if proposal is None:
                    raise CoordinatorError(f"unknown topology proposal {proposal_key!r}")
                status = str(proposal.get("status", "pending"))
                if status != "pending":
                    raise CoordinatorError(
                        f"topology proposal {proposal_key!r} is {status}"
                    )
                if (int(proposal.get("membership_epoch", -1))
                        != int(saved.get("membership_epoch", 0))
                        or int(proposal.get("base_revision", -1))
                        != int(saved.get("topology_revision", 0))):
                    raise CoordinatorError("topology proposal is stale")
                peer_ids = saved.get("peers", {})
                voters = proposal.get("voters", [])
                if voter not in voters or voter not in peer_ids:
                    raise CoordinatorError(f"peer {voter!r} is not eligible to vote")
                votes = proposal.setdefault("votes", {})
                previous = votes.get(voter)
                if previous is not None and previous != approve:
                    raise CoordinatorError(f"peer {voter!r} already cast the opposite vote")
                votes[voter] = approve
                quorum = len(voters) // 2 + 1
                approvals = sum(bool(value) for value in votes.values())
                rejections = len(votes) - approvals
                if approvals >= quorum:
                    proposal["status"] = "committed"
                    saved["topology_revision"] = int(saved.get("topology_revision", 0)) + 1
                    saved["topology"] = list(proposal["members"])
                    saved["revoked_members"] = [
                        member for member in saved.get("revoked_members", [])
                        if member not in proposal["members"]
                    ]
                    saved["dispatch_cursor"] = 0
                    for other in proposals:
                        if (other.get("proposal_id") != proposal_key
                                and other.get("status") == "pending"):
                            other["status"] = "stale"
                elif rejections > len(voters) - quorum:
                    proposal["status"] = "rejected"
                return saved

            self.task_store.mutate_runtime_state("coordinator", record_vote)
            self._refresh()
            return self._proposal_view(self._proposals[proposal_key])

    def proposal(self, proposal_id: str) -> dict[str, Any]:
        key = self._identity(proposal_id, "proposal_id")
        with self._lock:
            self._refresh()
            try:
                return self._proposal_view(self._proposals[key])
            except KeyError as exc:
                raise CoordinatorError(f"unknown topology proposal {key!r}") from exc

    def submit_task(self, task: dict[str, Any], *, actor: str = "coordinator",
                    idempotency_key: str | None = None) -> TaskSubmission:
        """Validate and persist through an eligible peer's WorkerPool."""
        with self._lock:
            self._refresh()
            candidate_ids = (self._topology if self._topology
                             else tuple(sorted(self._peers)))
            for peer_id in candidate_ids:
                peer = self._peers.get(peer_id)
                if peer is None:
                    continue
                try:
                    worker = peer.pool.worker(peer.worker_id)
                except WorkerPoolError:
                    continue
                try:
                    peer.pool.adapters.select(task, worker["cli_profile"])
                except NoAdapterError:
                    continue
                saved, replayed = peer.pool.submit_task(
                    task, actor=actor, idempotency_key=idempotency_key
                )
                return TaskSubmission(peer_id, copy.deepcopy(saved), replayed)
        raise NoAdapterError("no registered topology peer has an adapter for this task")

    def dispatch_next(self, *, now: float | None = None) -> Dispatch | None:
        """Try committed peers in agreed round-robin order until one claims work."""
        with self._lock:
            self._refresh()
            if not self._topology:
                return None
            count = len(self._topology)
            for offset in range(count):
                index = (self._dispatch_cursor + offset) % count
                peer_id = self._topology[index]
                if peer_id in self._revoked_members:
                    continue
                peer = self._peers.get(peer_id)
                if peer is None:
                    continue
                try:
                    view = peer.pool.worker(peer.worker_id)
                except WorkerPoolError:
                    continue
                if view["status"] != WorkerStatus.IDLE.value:
                    continue
                try:
                    claimed = peer.pool.claim_next(peer.worker_id, now=now)
                except WorkerPoolError:
                    # A concurrent pool transition can make an idle worker busy
                    # after the snapshot; only that race is treated as a skip.
                    try:
                        current = peer.pool.worker(peer.worker_id)
                    except WorkerPoolError:
                        continue
                    if current["status"] != WorkerStatus.IDLE.value:
                        continue
                    raise
                if claimed is not None:
                    self._dispatch_cursor = (index + 1) % count
                    self._persist()
                    return Dispatch(peer_id, peer.worker_id, claimed)
            return None

    def _membership_changed(self) -> None:
        self._membership_epoch += 1
        self._stale_pending()

    def _refresh(self) -> None:
        """Merge the latest shared coordinator record before cross-role actions."""
        saved = self.task_store.get_runtime_state("coordinator", {})
        if not isinstance(saved, dict):
            raise CoordinatorError("persisted coordinator state is not an object")
        descriptors = saved.get("peers", {})
        if isinstance(descriptors, dict):
            self._peer_descriptors = {str(k): str(v) for k, v in descriptors.items()}
        for row in saved.get("proposals", []):
            if not isinstance(row, dict) or not isinstance(row.get("proposal_id"), str):
                raise CoordinatorError("persisted coordinator proposal is invalid")
            proposal = _Proposal(
                proposal_id=row["proposal_id"],
                base_revision=int(row["base_revision"]),
                membership_epoch=int(row["membership_epoch"]),
                members=tuple(row["members"]), voters=tuple(row["voters"]),
                votes=dict(row.get("votes", {})), status=str(row.get("status", "pending")),
            )
            self._proposals[proposal.proposal_id] = proposal
        self._membership_epoch = int(saved.get("membership_epoch", self._membership_epoch))
        self._topology_revision = int(saved.get("topology_revision", self._topology_revision))
        self._topology = tuple(saved.get("topology", self._topology))
        self._revoked_members = set(saved.get("revoked_members", self._revoked_members))
        self._dispatch_cursor = int(saved.get("dispatch_cursor", self._dispatch_cursor))

    def _persist(self) -> None:
        """Persist consensus state transactionally; live WorkerPool objects stay local."""
        self.task_store.set_runtime_state("coordinator", {
            "peers": dict(sorted(self._peer_descriptors.items())),
            "membership_epoch": self._membership_epoch,
            "topology_revision": self._topology_revision,
            "topology": list(self._topology),
            "revoked_members": sorted(self._revoked_members),
            "dispatch_cursor": self._dispatch_cursor,
            "proposals": [self._proposal_view(self._proposals[key])
                          for key in sorted(self._proposals)],
        })

    def _stale_pending(self, except_id: str | None = None) -> None:
        for proposal in self._proposals.values():
            if proposal.proposal_id != except_id and proposal.status == "pending":
                proposal.status = "stale"

    def _require_peer(self, peer_id: str) -> _Peer:
        try:
            return self._peers[peer_id]
        except KeyError as exc:
            raise CoordinatorError(f"peer {peer_id!r} is not registered") from exc

    @staticmethod
    def _identity(value: Any, field_name: str) -> str:
        if not isinstance(value, str):
            raise CoordinatorError(f"{field_name} must be a string")
        identity = value.strip()
        if not identity:
            raise CoordinatorError(f"{field_name} cannot be empty")
        return identity

    @staticmethod
    def _peer_view(peer: _Peer) -> dict[str, Any]:
        try:
            worker = peer.pool.worker(peer.worker_id)
            status = worker["status"]
            profile = worker["cli_profile"]
            active_task_id = worker["active_task_id"]
        except WorkerPoolError:
            status, profile, active_task_id = "unavailable", None, None
        return {"peer_id": peer.peer_id, "worker_id": peer.worker_id,
                "cli_profile": profile, "status": status,
                "active_task_id": active_task_id}

    @staticmethod
    def _proposal_view(proposal: _Proposal) -> dict[str, Any]:
        quorum = len(proposal.voters) // 2 + 1
        return {
            "proposal_id": proposal.proposal_id,
            "base_revision": proposal.base_revision,
            "membership_epoch": proposal.membership_epoch,
            "members": list(proposal.members),
            "voters": list(proposal.voters),
            "votes": dict(sorted(proposal.votes.items())),
            "quorum": quorum,
            "status": proposal.status,
        }
