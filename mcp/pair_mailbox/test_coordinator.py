"""Coordinator membership, topology consensus, and WorkerPool dispatch tests."""
from __future__ import annotations

import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))

from coordinator import CoordinatorError, MultiAgentCoordinator  # noqa: E402
from server import Store  # noqa: E402
from task_store import TaskStoreError  # noqa: E402
from worker_pool import AdapterRegistry, NoAdapterError, WorkerPool  # noqa: E402


class TestMultiAgentCoordinator(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / ".pair"
        self.store = Store(self.root, allow_create=True)
        self.registry = AdapterRegistry()
        self.registry.register("codex", lambda task: task["text"], {"python"})
        self.registry.register("claude", lambda task: task["text"], {"browser"})
        self.coordinator = MultiAgentCoordinator(self.store.task_store)
        self.pools: list[WorkerPool] = []
        self.pool = self.make_pool(self.store)

    def tearDown(self) -> None:
        for pool in self.pools:
            for worker in pool.workers():
                pool.stop_heartbeats(worker["worker_id"], timeout=1)
        self.tmp.cleanup()

    def make_pool(self, store: Store) -> WorkerPool:
        pool = WorkerPool(store.task_store, self.registry, lease_seconds=30,
                          heartbeat_timeout=10)
        self.pools.append(pool)
        return pool

    def register(self, peer_id: str, worker_id: str, *, profile: str = "codex",
                 pool: WorkerPool | None = None) -> None:
        endpoint = pool or self.pool
        endpoint.register_worker(worker_id, profile, now=100)
        self.coordinator.register_peer(peer_id, endpoint, worker_id)

    def task(self, text: str = "job", *, profile: str = "codex",
             capabilities: tuple[str, ...] = ()) -> dict:
        return {
            "id": None,
            "text": text,
            "status": "open",
            "added_by": "test",
            "added_at": "fixed",
            "round": 7,
            "reason": "",
            "closed_at": None,
            "closed_by": None,
            "cli_profile": profile,
            "execution_requirements": {"capabilities": list(capabilities)},
            "execution_status": "ready",
        }

    def submit(self, text: str = "job", *, profile: str = "codex",
               capabilities: tuple[str, ...] = (),
               idempotency_key: str | None = None):
        return self.coordinator.submit_task(
            self.task(text, profile=profile, capabilities=capabilities),
            idempotency_key=idempotency_key,
        )

    def agree(self, members: list[str], voters: list[str] | None = None) -> dict:
        voter_ids = voters or sorted(peer["peer_id"] for peer in self.coordinator.peers())
        proposal = self.coordinator.propose_topology(voter_ids[0], members)
        result = proposal
        for voter in voter_ids[:proposal["quorum"]]:
            result = self.coordinator.vote(proposal["proposal_id"], voter, True)
        return result

    def test_registration_requires_known_idle_worker_and_shared_store(self) -> None:
        with self.assertRaisesRegex(CoordinatorError, "not registered"):
            self.coordinator.register_peer("peer-a", self.pool, "missing")

        other_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(other_tmp.cleanup)
        other_store = Store(Path(other_tmp.name) / ".pair", allow_create=True)
        other_pool = self.make_pool(other_store)
        with self.assertRaisesRegex(CoordinatorError, "share the coordinator TaskStore"):
            self.coordinator.register_peer("peer-a", other_pool, "worker-a")

    def test_peer_and_worker_endpoint_are_unique(self) -> None:
        self.register("peer-a", "worker-a")
        with self.assertRaisesRegex(CoordinatorError, "already registered"):
            self.coordinator.register_peer("peer-a", self.pool, "worker-a")
        with self.assertRaisesRegex(CoordinatorError, "already registered as a peer"):
            self.coordinator.register_peer("peer-b", self.pool, "worker-a")

    def test_topology_commits_after_strict_majority_and_preserves_order(self) -> None:
        for index in range(3):
            self.register(f"peer-{index}", f"worker-{index}")
        proposal = self.coordinator.propose_topology("peer-0", ["peer-2", "peer-0"])

        pending = self.coordinator.vote(proposal["proposal_id"], "peer-0", True)
        repeated = self.coordinator.vote(proposal["proposal_id"], "peer-0", True)
        self.assertEqual("pending", pending["status"])
        self.assertEqual(pending, repeated)

        committed = self.coordinator.vote(proposal["proposal_id"], "peer-1", True)

        self.assertEqual("committed", committed["status"])
        self.assertEqual(2, committed["quorum"])
        self.assertEqual({"peers": ["peer-2", "peer-0"], "revision": 1,
                          "membership_epoch": 3, "inactive_peers": []},
                         self.coordinator.topology())

    def test_rejection_quorum_and_opposite_vote_are_enforced(self) -> None:
        for index in range(3):
            self.register(f"peer-{index}", f"worker-{index}")
        proposal = self.coordinator.propose_topology("peer-0", ["peer-0"])
        with self.assertRaisesRegex(CoordinatorError, "not eligible"):
            self.coordinator.vote(proposal["proposal_id"], "unknown", True)
        self.coordinator.vote(proposal["proposal_id"], "peer-0", False)

        with self.assertRaisesRegex(CoordinatorError, "opposite vote"):
            self.coordinator.vote(proposal["proposal_id"], "peer-0", True)
        rejected = self.coordinator.vote(proposal["proposal_id"], "peer-1", False)

        self.assertEqual("rejected", rejected["status"])
        self.assertEqual(0, self.coordinator.topology()["revision"])

    def test_membership_change_stales_pending_proposal(self) -> None:
        self.register("peer-a", "worker-a")
        self.register("peer-b", "worker-b")
        proposal = self.coordinator.propose_topology("peer-a", ["peer-a"])
        self.register("peer-c", "worker-c")

        self.assertEqual("stale", self.coordinator.proposal(proposal["proposal_id"])["status"])
        with self.assertRaisesRegex(CoordinatorError, "is stale"):
            self.coordinator.vote(proposal["proposal_id"], "peer-a", True)

    def test_a_committed_revision_stales_competing_proposals(self) -> None:
        for index in range(3):
            self.register(f"peer-{index}", f"worker-{index}")
        first = self.coordinator.propose_topology("peer-0", ["peer-0"])
        second = self.coordinator.propose_topology("peer-1", ["peer-1"])
        self.coordinator.vote(first["proposal_id"], "peer-0", True)
        self.coordinator.vote(first["proposal_id"], "peer-1", True)

        self.assertEqual("stale", self.coordinator.proposal(second["proposal_id"])["status"])
        with self.assertRaisesRegex(CoordinatorError, "is stale"):
            self.coordinator.vote(second["proposal_id"], "peer-0", True)

    def test_dispatch_waits_for_consensus_and_uses_round_robin_order(self) -> None:
        self.register("peer-a", "worker-a")
        self.register("peer-b", "worker-b")
        self.submit("first")
        self.submit("second")

        self.assertIsNone(self.coordinator.dispatch_next())
        self.agree(["peer-b", "peer-a"])

        first = self.coordinator.dispatch_next(now=101)
        second = self.coordinator.dispatch_next(now=102)

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        assert first is not None and second is not None
        self.assertEqual("peer-b", first.peer_id)
        self.assertEqual("peer-a", second.peer_id)
        self.assertNotEqual(first.claimed.task["id"], second.claimed.task["id"])

    def test_dispatch_skips_incompatible_peer_and_governance_tasks(self) -> None:
        self.register("peer-claude", "worker-claude", profile="claude")
        self.register("peer-codex", "worker-codex")
        self.submit("python job", capabilities=("python",))
        self.agree(["peer-claude", "peer-codex"])

        dispatched = self.coordinator.dispatch_next(now=101)

        self.assertIsNotNone(dispatched)
        assert dispatched is not None
        self.assertEqual("peer-codex", dispatched.peer_id)

        self.store.task_store.add_task(
            {"id": None, "text": "pair governance", "status": "open", "round": 7},
            "test", "governance",
        )
        self.assertIsNone(self.coordinator.dispatch_next(now=102))

    def test_submit_routes_to_compatible_peer_and_rejects_unsupported_work(self) -> None:
        self.register("claude", "worker-claude", profile="claude")
        self.register("codex", "worker-codex")

        saved = self.submit("python work", capabilities=("python",))

        self.assertEqual("codex", saved.peer_id)
        self.assertEqual("open", saved.task["status"])
        with self.assertRaises(NoAdapterError):
            self.submit("unsupported", profile="unknown")
        self.assertEqual(1, len(self.store.task_store.list_tasks()))

    def test_task_submission_is_idempotent_in_the_shared_task_store(self) -> None:
        self.register("peer-a", "worker-a")

        first = self.submit("stable", idempotency_key="coordinator-add")
        replay = self.submit("stable", idempotency_key="coordinator-add")

        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(first.task["id"], replay.task["id"])
        self.assertEqual(1, len(self.store.task_store.list_tasks()))
        with self.assertRaises(TaskStoreError):
            self.submit("changed", idempotency_key="coordinator-add")

    def test_unregister_refuses_worker_with_active_task(self) -> None:
        self.register("peer-a", "worker-a")
        self.submit("work")
        self.assertIsNotNone(self.pool.claim_next("worker-a", now=101))

        with self.assertRaisesRegex(CoordinatorError, "active task"):
            self.coordinator.unregister_peer("peer-a")

    def test_two_registered_pools_share_taskstore_lease_authority(self) -> None:
        other_store = Store(self.root, allow_create=True)
        other_pool = self.make_pool(other_store)
        self.register("peer-a", "worker-a", pool=self.pool)
        self.register("peer-b", "worker-b", pool=other_pool)
        submitted = self.submit("shared job")
        self.agree(["peer-a", "peer-b"])

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(self.coordinator.dispatch_next, now=101)
                       for _ in range(2)]
            results = [future.result(timeout=5) for future in futures]

        claims = [result for result in results if result is not None]
        self.assertEqual(1, len(claims))
        self.assertEqual(submitted.task["id"], claims[0].claimed.task["id"])
        stored = self.store.task_store.list_tasks()[0]
        self.assertEqual("busy", stored["execution_status"])
        self.assertEqual(claims[0].worker_id, stored["worker_id"])

    def test_rejoining_peer_needs_a_new_topology_vote(self) -> None:
        self.register("peer-a", "worker-a")
        self.register("peer-b", "worker-b")
        self.agree(["peer-a", "peer-b"])
        self.submit("first")

        self.coordinator.unregister_peer("peer-b")
        self.register("peer-b", "worker-b-rejoined")
        self.assertEqual(["peer-b"], self.coordinator.topology()["inactive_peers"])
        first = self.coordinator.dispatch_next(now=101)
        self.assertIsNotNone(first)
        assert first is not None
        self.assertEqual("peer-a", first.peer_id)

        self.submit("second")
        self.agree(["peer-b", "peer-a"])
        second = self.coordinator.dispatch_next(now=102)

        self.assertIsNotNone(second)
        assert second is not None
        self.assertEqual("peer-b", second.peer_id)
        self.assertEqual([], self.coordinator.topology()["inactive_peers"])


if __name__ == "__main__":
    unittest.main()
