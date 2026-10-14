"""Server-freshness tests: server_freshness.py.

One test file per source: server_freshness.py -> test_server_freshness.py.
"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

import server_freshness
from pair_test_base import PairTestCase
from server import ProtocolError


class TestServerFreshness(unittest.TestCase):
    def test_process_start_time_is_positive(self) -> None:
        start_ts = server_freshness.get_process_start_time()
        self.assertGreater(start_ts, 0.0)

    def test_proc_available_matches_platform(self) -> None:
        """Round 92 C7: the helper reports /proc presence, not a guess."""
        self.assertEqual(server_freshness.proc_available(),
                         Path("/proc").is_dir())

    def test_fresh_when_start_time_after_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            f1 = tmp_path / "foo.py"
            f1.write_text("x = 1\n", encoding="utf-8")
            st = f1.stat().st_mtime
            
            # The process starts AFTER the files are created
            verdict = server_freshness.check_server_freshness(
                code_dir=tmp_path, process_start_ts=st + 10.0
            )
            self.assertTrue(verdict["fresh"])
            self.assertEqual("foo.py", verdict["newest_code_file"])
            self.assertEqual([], verdict["stale_files"])
            self.assertIn("fresh", verdict["why"])

    def test_stale_when_files_newer_than_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            f1 = tmp_path / "bar.py"
            f1.write_text("y = 2\n", encoding="utf-8")
            st = f1.stat().st_mtime

            # The process starts LONG BEFORE the files change (> 2s)
            verdict = server_freshness.check_server_freshness(
                code_dir=tmp_path, process_start_ts=st - 100.0
            )
            self.assertFalse(verdict["fresh"])
            self.assertEqual("bar.py", verdict["newest_code_file"])
            self.assertEqual(["bar.py"], verdict["stale_files"])
            self.assertIn("Restart the MCP server", verdict["why"])
            self.assertGreater(verdict["lag_seconds"], 90.0)

    def test_tolerance_window_considers_nearly_simultaneous_fresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            f1 = tmp_path / "baz.py"
            f1.write_text("z = 3\n", encoding="utf-8")
            st = f1.stat().st_mtime

            # A 1-second gap lands inside the 2.0-second tolerance
            verdict = server_freshness.check_server_freshness(
                code_dir=tmp_path, process_start_ts=st - 1.0
            )
            self.assertTrue(verdict["fresh"])
            self.assertEqual([], verdict["stale_files"])


class TestServerFreshnessWiring(PairTestCase):
    def test_pair_state_reports_server_freshness(self) -> None:
        st = self.writer.tool_pair_state()
        self.assertIn("server_fresh", st)
        self.assertIn("server_stale_files", st)
        self.assertIsInstance(st["server_fresh"], bool)
        self.assertIsInstance(st["server_stale_files"], list)

    def test_advance_refused_forward_when_stale(self) -> None:
        # Stubbing check_server_freshness to return stale
        orig = server_freshness.check_server_freshness
        try:
            server_freshness.check_server_freshness = lambda: {
                "fresh": False,
                "stale_files": ["server.py"],
                "newest_code_file": "server.py",
                "why": "code changed past start",
            }
            def set_writer(s: dict) -> None:
                s["writer"] = "writer-agent"
            self.store.mutate(set_writer)
            # A forward move (say scoping -> plan) must refuse
            with self.assertRaises(ProtocolError) as ctx:
                self.writer.tool_pair_advance("plan")
            self.assertIn("server went stale", str(ctx.exception))
            self.assertIn("server.py", str(ctx.exception.hint))
        finally:
            server_freshness.check_server_freshness = orig

    def test_advance_backward_allowed_when_stale(self) -> None:
        # Setting the implement phase
        def set_phase(s: dict) -> None:
            s["phase"] = "implement"
        self.store.mutate(set_phase)

        orig = server_freshness.check_server_freshness
        try:
            server_freshness.check_server_freshness = lambda: {
                "fresh": False,
                "stale_files": ["server.py"],
                "newest_code_file": "server.py",
                "why": "code changed past start",
            }
            # A backward step (implement -> plan) stays allowed on a stale server
            res = self.writer.tool_pair_advance("plan")
            self.assertEqual("plan", res["phase"])
        finally:
            server_freshness.check_server_freshness = orig

    def test_reopening_refused_when_stale(self) -> None:
        # Setting the done phase
        def set_done(s: dict) -> None:
            s["phase"] = "done"
            s["writer"] = "writer-agent"
        self.store.mutate(set_done)

        orig = server_freshness.check_server_freshness
        try:
            server_freshness.check_server_freshness = lambda: {
                "fresh": False,
                "stale_files": ["server.py"],
                "newest_code_file": "server.py",
                "why": "code changed past start",
            }
            # Reopening (done -> implement) blocks on stale
            with self.assertRaises(ProtocolError) as ctx:
                self.writer.tool_pair_advance("implement")
            self.assertIn("server went stale", str(ctx.exception))
        finally:
            server_freshness.check_server_freshness = orig


if __name__ == "__main__":
    unittest.main()
