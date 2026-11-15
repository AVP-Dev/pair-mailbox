"""Process-tree discovery tests used by the restart supervisor."""
from __future__ import annotations

import subprocess
import unittest
from unittest import mock

import process_tree


class TestDarwinProcessTree(unittest.TestCase):
    def test_process_table_is_walked_breadth_first(self) -> None:
        result = subprocess.CompletedProcess(
            ["/bin/ps"], 0, " 10  1\n 11 10\n 12 11\n 13 10\n", "")
        with mock.patch.object(process_tree.platform, "system", return_value="Darwin"), \
                mock.patch.object(process_tree.subprocess, "run", return_value=result) as run:
            self.assertEqual([11, 13, 12], process_tree.descendants(10))
        self.assertEqual(["/bin/ps", "-axo", "pid=,ppid="], run.call_args.args[0])

    def test_darwin_command_line_uses_libproc_identity(self) -> None:
        expected = {"pid": 123, "ppid": 10, "start": "birth", "argv": [
            "python3", "server.py", "--role", "critic"]}
        with mock.patch.object(process_tree.platform, "system", return_value="Darwin"), \
                mock.patch("launch_context.process_info", return_value=expected):
            self.assertEqual(expected["argv"], process_tree.cmdline(123))

    def test_unsupported_host_has_no_process_backend(self) -> None:
        with mock.patch.object(process_tree.platform, "system", return_value="Windows"):
            self.assertEqual([], process_tree.descendants(10))


if __name__ == "__main__":
    unittest.main(verbosity=2)
