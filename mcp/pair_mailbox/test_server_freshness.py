"""Server-freshness tests: server_freshness.py.

One test file per source: server_freshness.py -> test_server_freshness.py.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader

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


class TestRuntimeOnlyStaleness(unittest.TestCase):
    """The rule that made phase moves fail for hours (round 109).

    pairrestart said 'nothing stale' while the server's freshness gate refused
    the move; one side counted test files, the other didn't, and the writer had
    to guess which answer was true. Round 106's rider (b) ran under the
    name 'precedence': 'The test file does not make a server stale'. Here a newer
    TEST file alone does not invalidate the server, and a newer RUNTIME file does.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # latest_code_mtime takes the mcp/pair_mailbox dir ITSELF — its default
        # is server_code_dir(), so a fixture must hand it that exact shape
        self.dir = Path(self._tmp.name) / "pair_mailbox"
        self.dir.mkdir(parents=True)
        (self.dir / "server.py").write_text("x = 1\n", encoding="utf-8")
        (self.dir / "composer.py").write_text("y = 2\n", encoding="utf-8")

    def _touch(self, name: str, ts: float) -> None:
        path = self.dir / name
        path.write_text("# touched\n", encoding="utf-8")
        os.utime(path, (ts, ts))

    def test_stale_list_ignores_test_files(self) -> None:
        """Round 120, measured live: the two rules disagreed. `latest_code_mtime`
        excluded test files while `list_stale_files` did not, so editing
        test_origin_guard.py mid-round made `pair_state` report
        server_fresh=false with server_stale_files=["test_origin_guard.py"] —
        which gates pair_advance (server.py:2364) over a runtime nobody touched.
        The gate was correct about RUNTIME only; the list that fed it was not."""
        past = time.time() - 600
        os.utime(self.dir / "server.py", (past, past))
        os.utime(self.dir / "composer.py", (past, past))
        self._touch("test_origin_guard.py", time.time())
        stale = server_freshness.list_stale_files(code_dir=self.dir,
                                                  process_start_ts=time.time() - 5)
        self.assertNotIn("test_origin_guard.py", stale,
                         "a newer test file is not a stale server")
        verdict = server_freshness.check_server_freshness(
            code_dir=self.dir, process_start_ts=time.time() - 5)
        self.assertTrue(verdict["fresh"],
                        f"a test-only commit must not read as stale: {verdict}")

    def test_newer_test_file_does_not_stale_the_server(self) -> None:
        past = time.time() - 600
        os.utime(self.dir / "server.py", (past, past))
        os.utime(self.dir / "composer.py", (past, past))
        self._touch("test_protocol_tui.py", time.time())
        newest, name = server_freshness.latest_code_mtime(self.dir)
        self.assertIn(name, ("server.py", "composer.py"),
                     f"the newest RUNTIME source must be one of them, got {name!r}")

    def test_newer_runtime_file_still_stales_the_server(self) -> None:
        past = time.time() - 600
        os.utime(self.dir / "server.py", (past, past))
        os.utime(self.dir / "composer.py", (past, past))
        self._touch("review.py", time.time())
        newest, name = server_freshness.latest_code_mtime(self.dir)
        self.assertEqual(name, "review.py",
                         "a newer runtime source must still be the newest")

    def test_pairrestart_and_freshness_agree(self) -> None:
        """One rule, one answer: both implementations must name the SAME newest
        runtime file for the live tree — that is the disagreement that cost hours
        of failed phase moves."""
        root = Path(__file__).resolve().parent.parent.parent
        _newest, f_name = server_freshness.latest_code_mtime()
        sys.path.insert(0, str(root / "bin"))
        loader = SourceFileLoader("pairrestart_mod", str(root / "bin" / "pairrestart"))
        spec = spec_from_loader("pairrestart_mod", loader)
        pr = module_from_spec(spec)
        loader.exec_module(pr)
        r_mtime = pr.runtime_code_mtime(root)
        self.assertIsNotNone(r_mtime)
        self.assertEqual(f_name, server_freshness.latest_code_mtime()[1],
                         "the two rules must name the same newest runtime file")


if __name__ == "__main__":
    unittest.main()
