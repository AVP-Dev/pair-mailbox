"""Worker routing, leases, heartbeat recovery, and preemption tests."""
from __future__ import annotations

import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))

from server import Store  # noqa: E402
from worker_pool import AdapterRegistry, WorkerPool, WorkerPoolError, WorkerStatus  # noqa: E402


class _FakeProcess:
    def __init__(self, *, needs_kill: bool = False) -> None:
        self.needs_kill = needs_kill
        self.killed = False
        self.exited = False
        self.events: list[str] = []

    def poll(self) -> int | None:
        return 0 if self.exited else None

    def terminate(self) -> None:
        self.events.append("SIGTERM")
        if not self.needs_kill:
            self.exited = True

    def wait(self, timeout: float | None = None) -> int:
        self.events.append("wait")
        if self.needs_kill and not self.killed:
            raise subprocess.TimeoutExpired("fake-worker", timeout)
        self.exited = True
        return 0

    def kill(self) -> None:
        self.events.append("SIGKILL")
        self.killed = True


class TestWorkerPool(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / ".pair"
        self.store = Store(self.root, allow_create=True)
        self.registry = AdapterRegistry()
        self.codex_adapter = self.registry.register(
            "codex", lambda task: task["text"], capabilities={"python", "shell"})
        self.registry.register("claude", lambda task: task["text"], capabilities={"browser"})
        self.pool = WorkerPool(
            self.store.task_store, self.registry, lease_seconds=30,
            heartbeat_timeout=10, preempt_grace_seconds=0.01,
        )
        self.extra_pools: list[WorkerPool] = []

    def tearDown(self) -> None:
        for pool in [self.pool, *self.extra_pools]:
            for worker in pool.workers():
                pool.stop_heartbeats(worker["worker_id"], timeout=1)
        self.tmp.cleanup()

    def submit(self, *, text: str = "worker job", profile: str = "codex",
               capabilities: tuple[str, ...] = (), **extra):
        task = {
            "id": None,
            "text": text,
            "status": "open",
            "added_by": "writer",
            "added_at": "fixed",
            "round": 7,
            "reason": "",
            "closed_at": None,
            "closed_by": None,
            "cli_profile": profile,
            "execution_requirements": {"capabilities": list(capabilities)},
            "execution_status": "ready",
        }
        task.update(extra)
        return self.pool.submit_task(task)[0]

    def test_adapter_registry_routes_by_profile_and_capability(self) -> None:
        task = self.submit(profile="claude", capabilities=("browser",))
        self.pool.register_worker("codex-worker", "codex", now=100)
        self.pool.register_worker("claude-worker", "claude", now=100)

        self.assertIsNone(self.pool.claim_next("codex-worker", now=101))
        claimed = self.pool.claim_next("claude-worker", now=101)

        self.assertIsNotNone(claimed)
        assert claimed is not None
        self.assertEqual(task["id"], claimed.task["id"])
        self.assertEqual("claude", claimed.adapter.cli_profile)
        self.assertEqual(WorkerStatus.BUSY.value, self.pool.worker("claude-worker")["status"])

    def test_claimed_task_cannot_mutate_pool_lease_state(self) -> None:
        self.submit(capabilities=("python",))
        self.pool.register_worker("codex-worker", "codex", now=100)
        claimed = self.pool.claim_next("codex-worker", now=101)
        assert claimed is not None
        claimed.task["id"] = 999
        claimed.lease["lease_token"] = 999

        done = self.pool.complete_task("codex-worker", now=102)

        self.assertEqual("done", done["status"])
        self.assertEqual(1, done["id"])

    def test_governance_tasks_are_not_worker_jobs(self) -> None:
        self.store.task_store.add_task(
            {"id": None, "text": "pair governance task", "status": "open", "round": 7},
            "writer", "governance-task",
        )
        self.pool.register_worker("codex-worker", "codex", now=100)

        self.assertIsNone(self.pool.claim_next("codex-worker", now=101))

    def test_concurrent_pools_cannot_claim_one_task_twice(self) -> None:
        task = self.submit(capabilities=("python",))
        other_store = Store(self.root, allow_create=True)
        first = WorkerPool(self.store.task_store, self.registry, lease_seconds=30,
                           heartbeat_timeout=10)
        second = WorkerPool(other_store.task_store, self.registry, lease_seconds=30,
                            heartbeat_timeout=10)
        self.extra_pools.extend([first, second])
        first.register_worker("worker-a", "codex", now=100)
        second.register_worker("worker-b", "codex", now=100)

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(first.claim_next, "worker-a", now=101),
                       executor.submit(second.claim_next, "worker-b", now=101)]
            results = [future.result(timeout=5) for future in futures]

        claimed = [result for result in results if result is not None]
        self.assertEqual(1, len(claimed))
        self.assertEqual(task["id"], claimed[0].task["id"])
        self.assertEqual("busy", self.store.task_store.list_tasks()[0]["execution_status"])

    def test_heartbeat_renews_lease_and_expired_worker_is_reclaimed(self) -> None:
        task = self.submit(capabilities=("python",), max_attempts=4)
        process = _FakeProcess()
        self.pool.register_worker("worker-a", "codex", process=process, now=100)
        initial = self.pool.claim_next("worker-a", now=100)
        assert initial is not None

        beat = self.pool.heartbeat("worker-a", now=109)
        self.assertEqual(139, beat["lease_expires_at"])
        self.assertEqual([], self.pool.reap_stale_workers(now=118))
        self.assertEqual(["worker-a"], self.pool.reap_stale_workers(now=120))
        self.assertEqual(["SIGTERM", "wait"], process.events)
        self.assertEqual("busy", self.store.task_store.list_tasks()[0]["execution_status"])

        self.pool.register_worker("worker-b", "codex", now=129)
        self.assertIsNone(self.pool.claim_next("worker-b", now=130))
        self.pool.heartbeat("worker-b", now=138)
        replacement = self.pool.claim_next("worker-b", now=139)

        self.assertIsNotNone(replacement)
        assert replacement is not None
        self.assertEqual(task["id"], replacement.task["id"])
        self.assertEqual(2, replacement.task["attempts"])
        self.assertEqual("busy", replacement.task["execution_status"])
        self.assertEqual(WorkerStatus.CRASHED.value, self.pool.worker("worker-a")["status"])

    def test_background_heartbeat_renews_active_lease(self) -> None:
        self.submit(capabilities=("python",))
        self.pool.register_worker("worker-a", "codex", heartbeat_interval=0.01)
        claimed = self.pool.claim_next("worker-a")
        assert claimed is not None
        initial_expiry = claimed.lease["expires_at"]

        try:
            time.sleep(0.06)
        finally:
            stopped = self.pool.stop_heartbeats("worker-a", timeout=1)

        self.assertTrue(stopped)
        self.assertGreater(self.pool.worker("worker-a")["lease_expires_at"], initial_expiry)

    def test_preemption_uses_sigterm_then_sigkill_and_requeues(self) -> None:
        task = self.submit(capabilities=("python",), max_attempts=3)
        process = _FakeProcess(needs_kill=True)
        self.pool.register_worker("worker-a", "codex", process=process, now=100)
        self.pool.register_worker("worker-b", "codex", now=100)
        self.assertIsNotNone(self.pool.claim_next("worker-a", now=100))

        view = self.pool.preempt_worker("worker-a", now=101)
        replacement = self.pool.claim_next("worker-b", now=102)

        self.assertEqual(WorkerStatus.DRAINING.value, view["status"])
        self.assertEqual(["SIGTERM", "wait", "SIGKILL", "wait"], process.events)
        self.assertIsNotNone(replacement)
        assert replacement is not None
        self.assertEqual(task["id"], replacement.task["id"])
        self.assertEqual("worker-b", replacement.task["worker_id"])
        self.assertEqual("busy", replacement.task["execution_status"])

    def test_exhausted_failure_stays_out_of_the_ready_queue(self) -> None:
        self.submit(capabilities=("python",), max_attempts=1)
        self.pool.register_worker("worker-a", "codex", now=100)
        self.pool.register_worker("worker-b", "codex", now=100)
        self.assertIsNotNone(self.pool.claim_next("worker-a", now=100))

        failed = self.pool.fail_task("worker-a", "adapter exited", now=101)

        self.assertEqual("failed", failed["execution_status"])
        self.assertEqual("adapter exited", failed["last_error"])
        self.assertIsNone(self.pool.claim_next("worker-b", now=102))

    def test_expired_final_attempt_is_failed_instead_of_requeued(self) -> None:
        self.submit(capabilities=("python",), max_attempts=1)
        process = _FakeProcess()
        self.pool.register_worker("worker-a", "codex", process=process, now=100)
        self.assertIsNotNone(self.pool.claim_next("worker-a", now=100))
        self.pool.register_worker("worker-b", "codex", now=129)

        self.assertIsNone(self.pool.claim_next("worker-b", now=130))
        failed = self.store.task_store.list_tasks()[0]

        self.assertEqual("failed", failed["execution_status"])
        self.assertEqual("lease expired after maximum attempts", failed["last_error"])

    def test_completion_closes_task_and_returns_worker_to_idle(self) -> None:
        self.submit(capabilities=("python",))
        self.pool.register_worker("worker-a", "codex", now=100)
        self.assertIsNotNone(self.pool.claim_next("worker-a", now=100))

        done = self.pool.complete_task("worker-a", result="ok", now=101)

        self.assertEqual("done", done["status"])
        self.assertEqual("done", done["execution_status"])
        self.assertEqual("ok", done["result"])
        self.assertEqual(WorkerStatus.IDLE.value, self.pool.worker("worker-a")["status"])

    def test_pool_requires_lease_longer_than_worker_timeout(self) -> None:
        with self.assertRaisesRegex(ValueError, "shorter"):
            WorkerPool(self.store.task_store, self.registry, lease_seconds=10,
                       heartbeat_timeout=10)


if __name__ == "__main__":
    unittest.main()
