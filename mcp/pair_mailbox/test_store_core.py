from __future__ import annotations

import sys
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import store_core  # noqa: E402


class TestHeartbeatPersistence(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = store_core.Store(Path(self.tmp.name) / ".pair", allow_create=True)
        state = self.store.default_state()
        state["roles"] = {"codex": {"pid": 123, "last_seen": "old"}}
        self.store._save(state)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_heartbeat_only_mutation_keeps_large_state_file_and_reads_latest_time(self) -> None:
        before = self.store.state_path.read_bytes()
        with mock.patch.object(store_core, "atomic_write_json",
                               wraps=store_core.atomic_write_json) as write:
            self.store.mutate(lambda state: state["roles"]["codex"].update(
                last_seen="new"))

        self.assertEqual(before, self.store.state_path.read_bytes())
        self.assertEqual("new", self.store.read()["roles"]["codex"]["last_seen"])
        self.assertTrue(self.store.heartbeat_path.exists())
        self.assertEqual([self.store.heartbeat_path], [call.args[0] for call in write.call_args_list])
        self.assertEqual(self.store._load()["updated_at"],
                         json.loads(self.store.heartbeat_path.read_text())["updated_at"])

    def test_non_heartbeat_mutation_still_saves_full_state_and_absorbs_overlay(self) -> None:
        self.store.mutate(lambda state: state["roles"]["codex"].update(last_seen="new"))
        before = self.store.state_path.read_bytes()

        self.store.mutate(lambda state: state["roles"]["codex"].update(pid=456))

        self.assertNotEqual(before, self.store.state_path.read_bytes())
        self.assertFalse(self.store.heartbeat_path.exists())
        self.assertEqual("new", self.store.read()["roles"]["codex"]["last_seen"])
        self.assertEqual(456, self.store.read()["roles"]["codex"]["pid"])

    def test_noop_mutation_does_not_write_either_file(self) -> None:
        with mock.patch.object(store_core, "atomic_write_json",
                               wraps=store_core.atomic_write_json) as write:
            self.store.mutate(lambda state: None)
        write.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
