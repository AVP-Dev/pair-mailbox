"""Process-tree discovery tests used by the restart supervisor."""
from __future__ import annotations

from pathlib import Path
import unittest
from unittest import mock

import process_tree


class TestDarwinProcessTree(unittest.TestCase):
    def test_process_table_is_walked_breadth_first(self) -> None:
        rows = {10: {"ppid": 1}, 11: {"ppid": 10},
                12: {"ppid": 11}, 13: {"ppid": 10}}
        with mock.patch.object(process_tree.platform, "system", return_value="Darwin"), \
                mock.patch.object(process_tree, "process_ids", return_value=[10, 11, 12, 13]), \
                mock.patch.object(process_tree, "process_info",
                                  side_effect=lambda pid: rows[pid]):
            self.assertEqual([11, 13, 12], process_tree.descendants(10))

    def test_darwin_command_line_uses_libproc_identity(self) -> None:
        expected = {"pid": 123, "ppid": 10, "start": "birth", "argv": [
            "python3", "server.py", "--role", "critic"]}
        with mock.patch.object(process_tree.platform, "system", return_value="Darwin"), \
                mock.patch("launch_context.process_info", return_value=expected):
            self.assertEqual(expected["argv"], process_tree.cmdline(123))

    def test_darwin_process_ids_use_libproc(self) -> None:
        with mock.patch.object(process_tree.platform, "system", return_value="Darwin"), \
                mock.patch.object(process_tree, "_darwin_process_ids",
                                  return_value=[10, 11]) as enumerate_pids:
            self.assertEqual([10, 11], process_tree.process_ids())
        enumerate_pids.assert_called_once_with()
        with mock.patch.object(process_tree.platform, "system", return_value="Darwin"), \
                mock.patch.object(process_tree, "_darwin_process_ids",
                                  side_effect=PermissionError("denied")):
            with self.assertRaisesRegex(RuntimeError, "libproc: denied"):
                process_tree.process_ids()

    def test_process_start_time_uses_darwin_epoch_from_libproc(self) -> None:
        with mock.patch.object(process_tree.platform, "system", return_value="Darwin"), \
                mock.patch.object(process_tree, "process_info",
                                  return_value={"start": "1794916800.250000"}):
            self.assertEqual(1794916800.25, process_tree.process_start_time(123))

    def test_pair_server_inventory_filters_role_and_exact_state_with_spaces(self) -> None:
        root = Path("/tmp/project with space/.pair")
        rows = {
            1: {"argv": ["python", "/repo/server.py", "--role", "codex",
                         "--state", "/tmp/foreign/.pair"]},
            2: {"argv": ["python", "/repo/server.py", "--role", "agy",
                         "--state", str(root)]},
            3: {"argv": ["python", "/repo/server.py", "--role", "codex",
                         "--state", str(root)]},
        }
        with mock.patch.object(process_tree, "process_ids", return_value=[1, 2, 3]), \
                mock.patch.object(process_tree, "process_info",
                                  side_effect=lambda pid: rows[pid]), \
                mock.patch.object(process_tree, "process_start_time", return_value=1.5):
            found = process_tree.pair_mailbox_servers(root, "codex")
        self.assertEqual([{"pid": 3, "role": "codex", "start": 1.5}], found)

    def test_unsupported_host_has_no_process_backend(self) -> None:
        with mock.patch.object(process_tree.platform, "system", return_value="Windows"):
            self.assertEqual([], process_tree.descendants(10))


if __name__ == "__main__":
    unittest.main(verbosity=2)
