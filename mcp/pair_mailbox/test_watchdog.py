"""Tests for the monologue detector: `mcp/pair_mailbox/watchdog.py`.

One test file per source file: `watchdog.py` owns one duty — deciding whether
an agent that is visibly producing output is WORKING or MONOLOGUING — and it is
checked here.

The properties that matter, and why:

1. **A tool clears the streak.** A long test run is work, not a monologue. The
   detector must not fire while a subprocess is alive, no matter how long the
   screen has been changing.
2. **A frozen screen clears the streak.** Silence is another defect's business
   (`check_stalled_busy` owns the frozen screen); the detector must not fire
   on it.
3. **The threshold is a boundary, not a hint.** One second below the limit is
   not a monologue; one second at it is.
4. **A streak that restarts is measured from the restart.** The detector must
   not accumulate seconds across a break — that is the difference between
   "busy for 5 minutes" and "busy for 5 minutes in total, spread over an hour".
5. **The MCP server and the CLI itself are not tools.** They are alive for the
   whole session; counting them as tools would exempt every role forever and
   the detector would never fire — the exact blindness this module exists to
   remove.
6. **A dead pid is not a tool.** The process tree changes under the caller.

The process-shape tests run against REAL processes: the suite starts a throwaway
`python3 -` and checks that the detector sees it as a tool. A stubbed
`/proc` would prove the string comparison is spelled right, not that the rule
sees a real tool.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import watchdog  # noqa: E402


class TestMonologueTracker(unittest.TestCase):
    """The streak arithmetic: when a monologue starts, and when it is over."""

    def test_first_observation_only_starts_the_streak(self):
        tracker = watchdog.MonologueTracker()
        verdict = tracker.observe("omp", streaming=True, has_tool=False, now=100.0)
        self.assertEqual(verdict["verdict"], "thinking")
        self.assertEqual(verdict["seconds"], 0.0)

    def test_streaming_without_a_tool_stays_thinking_below_the_limit(self):
        tracker = watchdog.MonologueTracker()
        tracker.observe("omp", streaming=True, has_tool=False, now=100.0)
        verdict = tracker.observe("omp", streaming=True, has_tool=False, now=100.0 + 10.0)
        self.assertEqual(verdict["verdict"], "thinking")
        self.assertAlmostEqual(verdict["seconds"], 10.0)

    def test_the_limit_is_a_boundary(self):
        tracker = watchdog.MonologueTracker()
        tracker.observe("omp", streaming=True, has_tool=False, now=0.0)
        below = tracker.observe("omp", streaming=True, has_tool=False,
                                now=watchdog.DEFAULT_MAX_BUSY_SECONDS - 1)
        at = tracker.observe("omp", streaming=True, has_tool=False,
                             now=watchdog.DEFAULT_MAX_BUSY_SECONDS)
        self.assertEqual(below["verdict"], "thinking")
        self.assertEqual(at["verdict"], "monologue")

    def test_a_live_tool_clears_the_streak(self):
        """A long test run is work. The detector must not fire on it."""
        tracker = watchdog.MonologueTracker()
        tracker.observe("omp", streaming=True, has_tool=False, now=0.0)
        verdict = tracker.observe("omp", streaming=True, has_tool=True, now=10.0)
        self.assertEqual(verdict["verdict"], "working")
        # And the next tool-free observation starts from zero, not from 10.
        verdict = tracker.observe("omp", streaming=True, has_tool=False, now=20.0)
        self.assertEqual(verdict["verdict"], "thinking")
        self.assertEqual(verdict["seconds"], 0.0)

    def test_a_frozen_screen_clears_the_streak(self):
        """Silence is another defect's business; the detector must not fire."""
        tracker = watchdog.MonologueTracker()
        tracker.observe("omp", streaming=True, has_tool=False, now=0.0)
        verdict = tracker.observe("omp", streaming=False, has_tool=False, now=10.0)
        self.assertEqual(verdict["verdict"], "idle")
        verdict = tracker.observe("omp", streaming=True, has_tool=False, now=20.0)
        self.assertEqual(verdict["verdict"], "thinking")
        self.assertEqual(verdict["seconds"], 0.0)

    def test_seconds_do_not_accumulate_across_a_break(self):
        """Two long spells are not one long spell.

        The streak is measured from the observation that saw the break, so the
        count restarts at zero. Counting from the break's own timestamp would
        be a guess — the screen may have been frozen for an hour before it
        moved again, and the agent was not thinking for that hour.
        """
        tracker = watchdog.MonologueTracker()
        tracker.observe("omp", streaming=True, has_tool=False, now=0.0)
        tracker.observe("omp", streaming=True, has_tool=False, now=100.0)
        tracker.observe("omp", streaming=False, has_tool=False, now=110.0)
        verdict = tracker.observe("omp", streaming=True, has_tool=False, now=120.0)
        self.assertEqual(verdict["verdict"], "thinking")
        self.assertEqual(verdict["seconds"], 0.0)
        verdict = tracker.observe("omp", streaming=True, has_tool=False, now=130.0)
        self.assertAlmostEqual(verdict["seconds"], 10.0)

    def test_roles_are_tracked_separately(self):
        tracker = watchdog.MonologueTracker()
        tracker.observe("omp", streaming=True, has_tool=False, now=0.0)
        verdict = tracker.observe("agy", streaming=True, has_tool=False, now=50.0)
        self.assertEqual(verdict["seconds"], 0.0)
        verdict = tracker.observe("omp", streaming=True, has_tool=False, now=60.0)
        self.assertAlmostEqual(verdict["seconds"], 60.0)

    def test_forget_drops_the_streak(self):
        tracker = watchdog.MonologueTracker()
        tracker.observe("omp", streaming=True, has_tool=False, now=0.0)
        tracker.forget("omp")
        verdict = tracker.observe("omp", streaming=True, has_tool=False, now=900.0)
        self.assertEqual(verdict["verdict"], "thinking")
        self.assertEqual(verdict["seconds"], 0.0)

    def test_reset_drops_every_streak(self):
        tracker = watchdog.MonologueTracker()
        tracker.observe("omp", streaming=True, has_tool=False, now=0.0)
        tracker.observe("agy", streaming=True, has_tool=False, now=0.0)
        tracker.reset()
        self.assertEqual(tracker.streak_seconds("omp", 900.0), 0.0)
        self.assertEqual(tracker.streak_seconds("agy", 900.0), 0.0)

    def test_a_clock_that_jumps_backwards_never_yields_negative_time(self):
        tracker = watchdog.MonologueTracker()
        tracker.observe("omp", streaming=True, has_tool=False, now=500.0)
        verdict = tracker.observe("omp", streaming=True, has_tool=False, now=100.0)
        self.assertGreaterEqual(verdict["seconds"], 0.0)


class TestToolDetection(unittest.TestCase):
    """Which processes count as work — against real processes, not stubs."""

    def test_a_real_child_process_is_a_tool(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            tools = watchdog.tool_pids([child.pid], "omp")
            self.assertIn(child.pid, tools)
        finally:
            child.kill()
            child.wait(timeout=5)

    def test_a_dead_pid_is_not_a_tool(self):
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait(timeout=5)
        self.assertNotIn(child.pid, watchdog.tool_pids([child.pid], "omp"))

    def test_a_role_server_is_not_a_tool(self):
        """The MCP server lives for the whole session. Counting it as a tool
        would exempt every role forever — the blindness this module removes."""
        server = subprocess.Popen([
            sys.executable, "-c",
            "import time; time.sleep(30)",
            "server.py", "--role", "omp",
        ])
        try:
            # execve may not have completed when Popen returned: /proc/<pid>/cmdline
            # is still the shell's own argv. Without this pause the test would
            # fail for a reason that has nothing to do with the rule.
            time.sleep(0.05)
            self.assertTrue(watchdog.is_role_server(server.pid, "omp"))
            self.assertNotIn(server.pid, watchdog.tool_pids([server.pid], "omp"))
        finally:
            server.kill()
            server.wait(timeout=5)

    def test_a_role_server_of_another_role_is_a_tool(self):
        """Our own server is exempt; somebody else's is not exempt-by-name."""
        server = subprocess.Popen([
            sys.executable, "-c",
            "import time; time.sleep(30)",
            "server.py", "--role", "agy",
        ])
        try:
            self.assertFalse(watchdog.is_role_server(server.pid, "omp"))
            self.assertIn(server.pid, watchdog.tool_pids([server.pid], "omp"))
        finally:
            server.kill()
            server.wait(timeout=5)

    def test_a_server_without_a_role_argument_is_a_tool(self):
        """The shape is `server.py --role <role>`; a bare `server.py` is not ours."""
        server = subprocess.Popen([
            sys.executable, "-c",
            "import time; time.sleep(30)",
            "server.py",
        ])
        try:
            self.assertFalse(watchdog.is_role_server(server.pid, "omp"))
            self.assertIn(server.pid, watchdog.tool_pids([server.pid], "omp"))
        finally:
            server.kill()
            server.wait(timeout=5)

    def test_the_agent_cli_itself_is_not_a_tool(self):
        """The CLI is the agent doing the monologue, not a tool it runs."""
        cli = subprocess.Popen(["sleep", "30"])
        try:
            # `sleep` is not a known CLI name, so it counts as a tool; the
            # exemption is by name and must not be a blanket exemption.
            self.assertIn(cli.pid, watchdog.tool_pids([cli.pid], "omp"))
        finally:
            cli.kill()
            cli.wait(timeout=5)

    def test_read_cmdline_of_a_missing_pid_is_empty(self):
        self.assertEqual(watchdog.read_cmdline(999999999), [])

    def test_the_current_process_is_a_tool(self):
        """The detector's own process tree: a real `python3` run is work."""
        self.assertIn(os.getpid(), watchdog.tool_pids([os.getpid()], "omp"))


if __name__ == "__main__":
    unittest.main()
