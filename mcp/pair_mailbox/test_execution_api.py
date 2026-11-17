"""Integration checks for MCP-facing execution jobs and coordinator recovery."""
from __future__ import annotations

import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from execution_api import ExecutionAPI  # noqa: E402
from server import PairSession, ProtocolError, tool_definitions  # noqa: E402
from store_core import Store  # noqa: E402
from task_store import TaskStoreError  # noqa: E402


class TestExecutionIntegration(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / ".pair"
        self.store = Store(self.root, allow_create=True)
        self.session = PairSession("codex", self.store)
        self.session.tool_pair_claim("integration test writer")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_routed_job_claim_heartbeat_and_governance_fence(self) -> None:
        queued = self.session.tool_pair_task(
            "add", text="execute this job", cli_profile="codex",
            execution_requirements={"capabilities": []}, idempotency_key="job-add",
        )
        self.assertEqual("execution", queued["kind"])
        task_id = queued["task"]["id"]
        listing = self.session.tool_pair_task("list")
        self.assertNotIn(task_id, listing["open"])
        self.assertNotIn(task_id, [item["id"] for item in listing["tasks"]])
        self.session.tool_pair_execution("register", cli_profile="codex")
        claim = self.session.tool_pair_execution("claim")["claimed"]
        self.assertEqual(task_id, claim["task"]["id"])
        self.assertIn("lease_token", claim["lease"])
        with self.assertRaisesRegex(ProtocolError, "leased execution job"):
            self.session.tool_pair_task("done", task_id)
        heartbeat = self.session.tool_pair_execution("heartbeat")["worker"]
        self.assertEqual(task_id, heartbeat["active_task_id"])
        completed = self.session.tool_pair_execution(
            "complete", result="verified output"
        )["task"]
        self.assertEqual("done", completed["execution_status"])
        self.assertEqual("verified output", completed["result"])

    def test_coordinator_proposal_and_topology_survive_runtime_recreation(self) -> None:
        self.session.tool_pair_execution("register", cli_profile="codex")
        self.session.tool_pair_coordinator("register_peer")
        proposal = self.session.tool_pair_coordinator(
            "propose", members=["codex"]
        )["proposal"]

        recovered = ExecutionAPI(self.store.task_store, "codex")
        recovered.pool.register_worker("codex", "codex")
        peer = recovered.coordinator.register_peer("codex", recovered.pool, "codex")
        self.assertEqual("codex", peer["peer_id"])
        self.assertEqual(proposal["proposal_id"],
                         recovered.coordinator.proposal(proposal["proposal_id"])["proposal_id"])
        committed = recovered.coordinator.vote(proposal["proposal_id"], "codex", True)
        self.assertEqual("committed", committed["status"])
        again = ExecutionAPI(self.store.task_store, "agy")
        self.assertEqual(["codex"], again.coordinator.topology()["peers"])

    def test_two_role_coordinator_instances_share_consensus_votes(self) -> None:
        writer = self.session.execution
        critic = ExecutionAPI(self.store.task_store, "agy")
        writer.pool.register_worker("codex", "codex")
        critic.pool.register_worker("agy", "agy")
        writer.coordinator.register_peer("codex", writer.pool, "codex")
        critic.coordinator.register_peer("agy", critic.pool, "agy")
        proposal = writer.coordinator.propose_topology("codex", ["codex", "agy"])
        self.assertEqual(["agy", "codex"], proposal["voters"])
        first = writer.coordinator.vote(proposal["proposal_id"], "codex", True)
        self.assertEqual("pending", first["status"])
        second = critic.coordinator.vote(proposal["proposal_id"], "agy", True)
        self.assertEqual("committed", second["status"])

    def test_expired_worker_lease_is_recovered_after_runtime_restart(self) -> None:
        self.session.tool_pair_task("add", text="resume after crash", cli_profile="codex")
        self.session.tool_pair_execution("register", cli_profile="codex")
        first = self.session.tool_pair_execution("claim")["claimed"]
        restarted = ExecutionAPI(self.store.task_store, "codex")
        restart_time = first["lease"]["expires_at"] + 1
        restarted.pool.register_worker("codex", "codex", now=restart_time)
        recovered = restarted.pool.claim_next(
            "codex", now=restart_time
        )
        self.assertIsNotNone(recovered)
        self.assertGreater(recovered.lease["fencing_token"],
                           first["lease"]["fencing_token"])

    def test_mcp_schema_exposes_the_explicit_worker_operations(self) -> None:
        definitions = {item["name"]: item for item in tool_definitions()}
        self.assertIn("pair_execution", definitions)
        self.assertIn("pair_coordinator", definitions)
        task_properties = definitions["pair_task"]["inputSchema"]["properties"]
        self.assertIn("cli_profile", task_properties)
        self.assertIn("execution_requirements", task_properties)

    def test_worker_lease_timeout_can_be_tuned_for_slow_runtimes(self) -> None:
        with mock.patch.dict("os.environ", {
            "PAIR_WORKER_LEASE_SECONDS": "900",
            "PAIR_WORKER_HEARTBEAT_TIMEOUT_SECONDS": "240",
        }):
            tuned = ExecutionAPI(self.store.task_store, "codex")
        self.assertEqual(900, tuned.pool.lease_seconds)
        self.assertEqual(240, tuned.pool.heartbeat_timeout)

    def test_legacy_state_mutation_cannot_close_an_execution_job(self) -> None:
        queued = self.session.tool_pair_task(
            "add", text="keep the lease boundary", cli_profile="codex"
        )
        task_id = queued["task"]["id"]

        def bypass(state):
            state["tasks"][0]["status"] = "done"

        with self.assertRaisesRegex(TaskStoreError, "use the worker API"):
            self.store.mutate(bypass)


if __name__ == "__main__":
    unittest.main()
