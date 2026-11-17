"""Tests for SQLite task migration, fencing, and Store compatibility."""
from __future__ import annotations

import json
import os
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from unittest import mock

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))

from server import PairSession, ProtocolError  # noqa: E402
from store_core import Store  # noqa: E402
from task_store import StaleTaskFence, TaskStore, TaskStoreError  # noqa: E402
import server_freshness  # noqa: E402


class TestTaskStoreMigration(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / ".pair"
        self.root.mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write_state(self, payload: dict) -> bytes:
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
        (self.root / "state.json").write_bytes(raw)
        return raw

    def test_migrates_list_with_exact_backup_and_private_wal_database(self) -> None:
        tasks = [
            {"id": 2, "text": "closed", "status": "done", "added_by": "human",
             "round": 2, "closed_round": 3},
            {"id": 5, "text": "open", "status": "open", "added_by": "codex",
             "round": 4},
        ]
        raw = self.write_state({"round": 4, "tasks": tasks, "phase": "implement"})

        store = Store(self.root)

        self.assertEqual(raw, (self.root / "state.json.pre-state-db.bak").read_bytes())
        migrated = store.read()
        self.assertEqual([2, 5], [item["id"] for item in migrated["tasks"]])
        self.assertEqual([0, 0], [item["fencing_token"] for item in migrated["tasks"]])
        self.assertEqual("implement", migrated["phase"])
        connection = store.task_store._connect()
        try:
            self.assertEqual("wal", connection.execute("PRAGMA journal_mode").fetchone()[0].lower())
            self.assertEqual(2, connection.execute("PRAGMA synchronous").fetchone()[0])
        finally:
            connection.close()
        self.assertEqual(0, stat.S_IMODE((self.root / "state.db").stat().st_mode) & 0o077)

    def test_migrates_legacy_single_task_once(self) -> None:
        raw = self.write_state({"task": "legacy request", "round": 9,
                                "created_at": "2026-01-01T00:00:00Z"})

        first = Store(self.root)
        second = Store(self.root)

        self.assertEqual(raw, (self.root / "state.json.pre-state-db.bak").read_bytes())
        self.assertEqual("legacy request", first.read()["tasks"][0]["text"])
        self.assertEqual([1], [item["id"] for item in second.read()["tasks"]])

    def test_pre_cutover_interruption_recovers_from_exact_backup(self) -> None:
        self.write_state({"tasks": [{"id": 1, "text": "keep", "status": "open"}]})
        with mock.patch.object(TaskStore, "_create_database", side_effect=OSError("crash")):
            with self.assertRaisesRegex(OSError, "crash"):
                Store(self.root)

        recovered = Store(self.root)

        self.assertEqual("keep", recovered.read()["tasks"][0]["text"])
        marker = json.loads((self.root / "state.db.cutover.json").read_text())
        self.assertEqual("complete", marker["status"])

    def test_reconciles_commit_before_complete_marker_after_crash(self) -> None:
        self.write_state({"tasks": [{"id": 1, "text": "committed", "status": "open"}]})
        original = TaskStore._write_marker

        def crash_before_complete(instance, marker):
            if marker.get("status") == "complete":
                raise OSError("crash after database commit")
            return original(instance, marker)

        with mock.patch.object(TaskStore, "_write_marker", crash_before_complete):
            with self.assertRaisesRegex(OSError, "database commit"):
                Store(self.root)

        recovered = Store(self.root)

        self.assertEqual("committed", recovered.read()["tasks"][0]["text"])
        marker = json.loads((self.root / "state.db.cutover.json").read_text())
        self.assertEqual("complete", marker["status"])

    def test_sigkill_during_uncommitted_wal_transaction_recovers_old_row(self) -> None:
        if os.name != "posix":
            self.skipTest("SIGKILL recovery test requires a POSIX process")
        self.write_state({"tasks": [{"id": 1, "text": "durable", "status": "open"}]})
        initial = Store(self.root).read()["tasks"][0]
        ready = self.root / "writer-ready"
        child = """
import json
import sys
import time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from store_core import Store
root, ready = Path(sys.argv[2]), Path(sys.argv[3])
store = Store(root)
connection = store.task_store._connect()
connection.execute("BEGIN IMMEDIATE")
row = connection.execute("SELECT payload_json FROM tasks WHERE id=1").fetchone()
task = json.loads(row[0])
task["text"] = "uncommitted write"
connection.execute("UPDATE tasks SET payload_json=? WHERE id=1",
                   (json.dumps(task, sort_keys=True),))
ready.touch()
while True:
    time.sleep(1)
"""
        process = subprocess.Popen(
            [sys.executable, "-c", child, str(Path(__file__).resolve().parent),
             str(self.root), str(ready)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(ready.exists(), "child did not reach its uncommitted update")
            os.kill(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)

        self.assertEqual(-signal.SIGKILL, process.returncode)
        recovered = Store(self.root).read()["tasks"][0]
        self.assertEqual(initial, recovered)

    def test_corruption_after_cutover_fails_closed_without_json_fallback(self) -> None:
        self.write_state({"tasks": [{"id": 1, "text": "stale copy", "status": "open"}]})
        Store(self.root)
        (self.root / "state.db").write_bytes(b"not a sqlite database")

        with self.assertRaisesRegex(TaskStoreError, "task operations frozen"):
            Store(self.root)


class TestTaskStoreTransitions(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / ".pair"
        self.store = Store(self.root, allow_create=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    @staticmethod
    def new_task(text: str = "task") -> dict:
        return {"id": None, "text": text, "status": "open", "added_by": "writer",
                "added_at": "fixed", "round": 7, "reason": "",
                "closed_at": None, "closed_by": None}

    def test_idempotent_add_and_close_return_original_transition(self) -> None:
        item, replayed = self.store.add_task(self.new_task(), "writer", "add-1")
        repeated, replayed_again = self.store.add_task(
            {**self.new_task(), "added_at": "new timestamp"}, "writer", "add-1")
        with self.assertRaisesRegex(TaskStoreError, "different request"):
            self.store.add_task(self.new_task("other task"), "writer", "add-1")

        closed, close_replay = self.store.close_task(
            item["id"], "done", "", "writer", expected_fencing_token=1,
            idempotency_key="close-1")
        repeated_close, repeated_close_flag = self.store.close_task(
            item["id"], "done", "", "writer", expected_fencing_token=1,
            idempotency_key="close-1")
        with self.assertRaises(StaleTaskFence):
            self.store.close_task(item["id"], "done", "", "writer",
                                  expected_fencing_token=1, idempotency_key="close-2")

        self.assertFalse(replayed)
        self.assertTrue(replayed_again)
        self.assertEqual(item, repeated)
        self.assertFalse(close_replay)
        self.assertTrue(repeated_close_flag)
        self.assertEqual(closed, repeated_close)
        self.assertEqual(2, self.store.task_store.transition_count())
        self.assertEqual(2, self.store.read()["tasks"][0]["fencing_token"])

    def test_expired_lease_cannot_renew_or_transition_after_reclaim(self) -> None:
        item, _ = self.store.add_task(self.new_task(), "writer", "add-lease")
        old, _ = self.store.task_store.acquire_lease(
            item["id"], "worker-a", 10, idempotency_key="lease-a", now=100)
        with self.assertRaises(StaleTaskFence):
            self.store.task_store.renew_lease(
                item["id"], "worker-a", old["lease_token"], 10,
                idempotency_key="renew-expired", now=111)
        current, _ = self.store.task_store.acquire_lease(
            item["id"], "worker-b", 10, idempotency_key="lease-b", now=111)

        with self.assertRaises(StaleTaskFence):
            self.store.task_store.transition_task(
                item["id"], {"status": "done"}, "worker-a", "complete",
                expected_fencing_token=old["fencing_token"],
                lease_token=old["lease_token"], idempotency_key="old-complete", now=112)
        with self.assertRaises(StaleTaskFence):
            self.store.task_store.renew_lease(
                item["id"], "worker-a", old["lease_token"], 10,
                idempotency_key="renew-stale", now=112)
        finished, replayed = self.store.task_store.transition_task(
            item["id"], {"status": "done"}, "worker-b", "complete",
            expected_fencing_token=current["fencing_token"],
            lease_token=current["lease_token"], idempotency_key="current-complete", now=112)

        self.assertFalse(replayed)
        self.assertEqual("done", finished["status"])
        self.assertEqual(current["fencing_token"] + 1, finished["fencing_token"])
        connection = self.store.task_store._connect()
        try:
            lease_owner, lease_expires_at = connection.execute(
                "SELECT lease_owner, lease_expires_at FROM tasks WHERE id=?",
                (item["id"],),
            ).fetchone()
        finally:
            connection.close()
        self.assertIsNone(lease_owner)
        self.assertIsNone(lease_expires_at)

    def test_release_lease_requeues_task_and_fences_previous_owner(self) -> None:
        item, _ = self.store.add_task(
            {**self.new_task(), "cli_profile": "codex",
             "execution_requirements": {"capabilities": ["python"]},
             "execution_status": "ready"},
            "writer", "release-lease-add",
        )
        lease, _ = self.store.task_store.acquire_lease(
            item["id"], "worker-a", 30, idempotency_key="release-lease-a", now=100)
        busy, _ = self.store.task_store.transition_task(
            item["id"], {"execution_status": "busy", "worker_id": "worker-a"},
            "worker-a", "worker_claim", lease["fencing_token"],
            lease_token=lease["lease_token"], idempotency_key="release-lease-claim", now=101)
        ready, _ = self.store.task_store.transition_task(
            item["id"], {"execution_status": "ready", "worker_id": None},
            "worker-a", "worker_retry", busy["fencing_token"],
            lease_token=lease["lease_token"], idempotency_key="release-lease-fail",
            now=102, release_lease=True)
        replayed_ready, was_replayed = self.store.task_store.transition_task(
            item["id"], {"execution_status": "ready", "worker_id": None},
            "worker-a", "worker_retry", busy["fencing_token"],
            lease_token=lease["lease_token"], idempotency_key="release-lease-fail",
            now=102, release_lease=True)

        replacement, _ = self.store.task_store.acquire_lease(
            item["id"], "worker-b", 30, idempotency_key="release-lease-b", now=103)

        self.assertEqual("ready", ready["execution_status"])
        self.assertTrue(was_replayed)
        self.assertEqual(ready, replayed_ready)
        self.assertEqual(lease["lease_token"] + 1, replacement["lease_token"])
        self.assertGreater(replacement["fencing_token"], busy["fencing_token"])
        with self.assertRaises(StaleTaskFence):
            self.store.task_store.transition_task(
                item["id"], {"execution_status": "done"}, "worker-a", "late-complete",
                busy["fencing_token"], lease_token=lease["lease_token"],
                idempotency_key="release-lease-late", now=104)

    def test_reclaim_expired_lease_returns_job_to_ready(self) -> None:
        item, _ = self.store.add_task(
            {**self.new_task(), "cli_profile": "codex", "execution_status": "ready"},
            "writer", "reclaim-lease-add",
        )
        lease, _ = self.store.task_store.acquire_lease(
            item["id"], "worker-a", 10, idempotency_key="reclaim-lease-a", now=100)
        busy, _ = self.store.task_store.transition_task(
            item["id"], {"execution_status": "busy", "worker_id": "worker-a"},
            "worker-a", "worker_claim", lease["fencing_token"],
            lease_token=lease["lease_token"], idempotency_key="reclaim-lease-claim", now=101)

        self.assertEqual([], self.store.task_store.reclaim_expired_leases(now=109))
        self.assertEqual([item["id"]], self.store.task_store.reclaim_expired_leases(now=110))
        self.assertEqual([], self.store.task_store.reclaim_expired_leases(now=111))
        ready = self.store.task_store.list_tasks()[0]

        self.assertEqual("ready", ready["execution_status"])
        self.assertIsNone(ready["worker_id"])
        self.assertGreater(ready["fencing_token"], busy["fencing_token"])
        with self.assertRaises(StaleTaskFence):
            self.store.task_store.transition_task(
                item["id"], {"execution_status": "done"}, "worker-a", "late-complete",
                busy["fencing_token"], lease_token=lease["lease_token"],
                idempotency_key="reclaim-lease-late", now=111)

    def test_reclaim_after_final_attempt_marks_job_failed(self) -> None:
        item, _ = self.store.add_task(
            {**self.new_task(), "cli_profile": "codex", "execution_status": "ready",
             "attempts": 1, "max_attempts": 1},
            "writer", "reclaim-exhausted-add",
        )
        lease, _ = self.store.task_store.acquire_lease(
            item["id"], "worker-a", 10, idempotency_key="reclaim-exhausted-a", now=100)
        self.store.task_store.transition_task(
            item["id"], {"execution_status": "busy", "worker_id": "worker-a"},
            "worker-a", "worker_claim", lease["fencing_token"],
            lease_token=lease["lease_token"], idempotency_key="reclaim-exhausted-claim", now=101)

        self.assertEqual([item["id"]], self.store.task_store.reclaim_expired_leases(now=110))
        failed = self.store.task_store.list_tasks()[0]

        self.assertEqual("failed", failed["execution_status"])
        self.assertEqual("lease expired after maximum attempts", failed["last_error"])

    def test_execution_metadata_is_part_of_add_idempotency(self) -> None:
        first, replayed = self.store.add_task(
            {**self.new_task(), "cli_profile": "codex",
             "execution_requirements": {"capabilities": ["python"]}},
            "writer", "execution-metadata-add",
        )

        self.assertFalse(replayed)
        self.assertEqual("codex", first["cli_profile"])
        with self.assertRaisesRegex(TaskStoreError, "different request"):
            self.store.add_task(
                {**self.new_task(), "cli_profile": "claude",
                 "execution_requirements": {"capabilities": ["python"]}},
                "writer", "execution-metadata-add",
            )

    def test_failed_transition_rolls_back_task_and_event_atomically(self) -> None:
        item, _ = self.store.add_task(self.new_task(), "writer", "atomic-add")
        with mock.patch.object(TaskStore, "_record", side_effect=sqlite3.IntegrityError(
                "injected event write failure")):
            with self.assertRaisesRegex(sqlite3.IntegrityError, "injected"):
                self.store.close_task(
                    item["id"], "done", "", "writer",
                    expected_fencing_token=item["fencing_token"],
                    idempotency_key="atomic-close",
                )

        after = self.store.read()["tasks"][0]
        self.assertEqual("open", after["status"])
        self.assertEqual(item["fencing_token"], after["fencing_token"])
        self.assertEqual(1, self.store.task_store.transition_count())

    def test_legacy_mutator_clears_lease_when_closing_task(self) -> None:
        item, _ = self.store.add_task(self.new_task(), "writer", "legacy-close-add")
        self.store.task_store.acquire_lease(
            item["id"], "worker", 60, idempotency_key="legacy-close-lease", now=100
        )
        self.store.mutate(lambda state: state["tasks"][0].update({"status": "done"}))

        connection = self.store.task_store._connect()
        try:
            lease_owner, lease_expires_at = connection.execute(
                "SELECT lease_owner, lease_expires_at FROM tasks WHERE id=?",
                (item["id"],),
            ).fetchone()
        finally:
            connection.close()
        self.assertIsNone(lease_owner)
        self.assertIsNone(lease_expires_at)

    def test_concurrent_transitions_use_compare_and_swap_fencing(self) -> None:
        item, _ = self.store.add_task(self.new_task(), "writer", "add-race")
        barrier = threading.Barrier(2)

        def attempt(label: str) -> str:
            barrier.wait()
            try:
                self.store.task_store.transition_task(
                    item["id"], {"text": label}, label, "update",
                    expected_fencing_token=item["fencing_token"],
                    idempotency_key=f"transition-{label}")
                return "applied"
            except StaleTaskFence:
                return "stale"

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(attempt, ("one", "two")))

        self.assertCountEqual(["applied", "stale"], outcomes)
        self.assertEqual(2, self.store.read()["tasks"][0]["fencing_token"])

    def test_legacy_store_mutate_callers_remain_db_backed(self) -> None:
        expected = {"id": 1, "text": "legacy mutator", "status": "open",
                    "added_at": "now", "added_by": "human", "round": 1}
        self.store.mutate(lambda state: state["tasks"].append(expected))

        reopened = Store(self.root)

        self.assertEqual("legacy mutator", reopened.read()["tasks"][0]["text"])
        self.assertEqual(1, reopened.read()["tasks"][0]["fencing_token"])


class TestToolPairTaskIntegration(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / ".pair"
        self.store = Store(self.root, allow_create=True)
        self.writer = PairSession("writer-agent", self.store)
        self.writer.tool_pair_claim(reason="integration test writer")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_pair_task_idempotency_fencing_drop_reason_and_banner(self) -> None:
        first = self.writer.tool_pair_task(
            "add", text="retry-safe task", idempotency_key="tool-add-1"
        )
        retry = self.writer.tool_pair_task(
            "add", text="retry-safe task", idempotency_key="tool-add-1"
        )
        self.assertEqual(first["task"], retry["task"])
        self.assertFalse(first["idempotent_replay"])
        self.assertTrue(retry["idempotent_replay"])
        self.assertEqual(1, len(self.store.read()["tasks"]))

        closed = self.writer.tool_pair_task(
            "done", first["task"]["id"], fencing_token=first["task"]["fencing_token"],
            idempotency_key="tool-close-1",
        )
        close_retry = self.writer.tool_pair_task(
            "done", first["task"]["id"], fencing_token=first["task"]["fencing_token"],
            idempotency_key="tool-close-1",
        )
        self.assertEqual(closed["task"], close_retry["task"])
        self.assertTrue(close_retry["idempotent_replay"])
        with self.assertRaises(ProtocolError) as stale:
            self.writer.tool_pair_task(
                "done", first["task"]["id"],
                fencing_token=first["task"]["fencing_token"],
                idempotency_key="tool-close-stale",
            )
        self.assertIn("fencing token is stale", str(stale.exception))
        self.assertIn("Read the current task fence", stale.exception.hint)

        second = self.writer.tool_pair_task("add", text="drop reason check")
        with self.assertRaises(ProtocolError) as no_reason:
            self.writer.tool_pair_task(
                "drop", second["task"]["id"], fencing_token=second["task"]["fencing_token"],
                idempotency_key="tool-drop-empty",
            )
        self.assertIn("cannot be dropped without a reason", str(no_reason.exception))
        self.writer.tool_pair_task(
            "drop", second["task"]["id"], reason="duplicate work",
            fencing_token=second["task"]["fencing_token"], idempotency_key="tool-drop-1",
        )
        self.assertEqual("", (self.root / "task-banner.txt").read_text(encoding="utf-8"))
        self.assertEqual("scoping", self.writer.tool_pair_state()["phase"])

    def test_task_add_and_done_gate_share_the_store_lock(self) -> None:
        self.store.mutate(lambda state: state.update({"phase": "verify", "mode": "audit"}))
        add_entered = threading.Event()
        allow_add = threading.Event()
        advance_started = threading.Event()
        original_add = self.store.task_store.add_task

        def pause_add(*args: Any, **kwargs: Any) -> Any:
            add_entered.set()
            if not allow_add.wait(5):
                raise TimeoutError("test did not release task add")
            return original_add(*args, **kwargs)

        def blockers_for_open_task(state: dict[str, Any], **_kwargs: Any) -> list[str]:
            return (["an open task remains"]
                    if any(item.get("status") == "open" for item in state.get("tasks", []))
                    else [])

        def advance() -> dict[str, Any]:
            advance_started.set()
            return self.writer.tool_pair_advance("done")

        with mock.patch.object(self.store.task_store, "add_task", side_effect=pause_add), \
                mock.patch.object(self.writer, "_done_blockers",
                                  side_effect=blockers_for_open_task), \
                mock.patch.object(server_freshness, "assert_server_fresh", return_value=None):
            with ThreadPoolExecutor(max_workers=2) as pool:
                adding = pool.submit(
                    self.writer.tool_pair_task, "add", text="arrives during close"
                )
                self.assertTrue(add_entered.wait(5), "task add did not acquire the store lock")
                finishing = pool.submit(advance)
                self.assertTrue(advance_started.wait(5), "done gate did not start")
                allow_add.set()
                added = adding.result(timeout=10)
                with self.assertRaises(ProtocolError) as refused:
                    finishing.result(timeout=10)

        self.assertIn("an open task remains", str(refused.exception))
        self.assertEqual("verify", self.store.read()["phase"])
        self.assertEqual([added["task"]["id"]], [
            item["id"] for item in self.store.read()["tasks"] if item["status"] == "open"
        ])


if __name__ == "__main__":
    unittest.main(verbosity=2)
