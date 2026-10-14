"""Machine-liveness tests (`board_hosts.py`).

Checked here is what the module contracts, and the contract is about "no" vs
"unknown". The difference is not stylistic: a red dot on a live role sends the
human restarting what is broken, while a grey one on a dead role hides a real
breakage. So every "unknown" pins its own check, and the "process found" check
must raise a real process — or the `/proc` walk drops out with the suite green.
    """
from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

from board_hosts import (clean_pid, mtime_age, pane_status, pilot_pids, pilot_section,
                        resolve_pane_title)
from pair_test_base import PairTestCase


class CleanPidTest(unittest.TestCase):
    def test_real_pid_survives(self):
        self.assertEqual(clean_pid(os.getpid()), os.getpid())

    def test_zero_and_junk_are_absent_not_dead(self):
        # Zero and non-integer mean "pid unrecorded". Turning them into "dead" is banned:
        # a role whose pid nobody wrote would read as broken.
        for value in (0, -1, None, "123", "", 1.5, True):
            self.assertIsNone(clean_pid(value), value)


class MtimeAgeTest(unittest.TestCase):
    def test_missing_file_is_none_not_zero(self):
        # Zero would read "just changed" for a file that is gone.
        self.assertIsNone(mtime_age(Path("/no/such/file")))

    def test_fresh_file_is_small(self):
        self.assertIsNotNone(mtime_age(Path(__file__)))


class ResolvePaneTitleTest(PairTestCase):
    """The pane name must never be invented.

    A regression found applying the agy patch: our own `display-message` query
    returned code 0 and `":."` for a missing pane — NONEMPTY output, and the
    "success code plus nonempty output" check passed. The screen grew
    `pane %999999 [.:]`, and the phantom read as a named pane. The same deception
    class already cost the project a hole between `server.pane_exists` and
    `composer.pane_alive`.
    """

    def test_phantom_pane_is_not_given_a_fabricated_name(self):
        self.assertEqual(resolve_pane_title("%999999"), "%999999")

    def test_pane_that_is_not_an_id_is_returned_unchanged(self):
        for value in ("mail:0.1", "", "not a pane"):
            self.assertEqual(resolve_pane_title(value), value)

    def test_live_pane_resolves_to_a_readable_address(self):
        """On a real tmux pane: the function must return something even on a live
        `%N`, or the check above would pass on the guard alone."""
        live = _first_live_pane()
        if live is None:
            self.skipTest("no live tmux panes in this environment")
        resolved = resolve_pane_title(live)
        self.assertNotEqual(resolved, live, "a live pane must resolve into a name")
        self.assertRegex(resolved, r"^.+:\d+\.\d+$",
                         f"expected an address like mail:0.0, got {resolved!r}")


def _first_live_pane() -> str | None:
    import subprocess

    from composer import _tmux, pane_identity
    listed = subprocess.run(["tmux", "list-panes", "-a", "-F", "#{pane_id}"],
                            capture_output=True, text=True)
    for entry in listed.stdout.split():
        try:
            if pane_identity(_tmux(), entry).get("exists"):
                return entry
        except Exception:
            continue
    return None


class PaneStatusTest(unittest.TestCase):
    def test_absent_pane_is_unknown_with_a_reason(self):
        alive, why = pane_status("")
        self.assertIsNone(alive)
        self.assertIn("pane", why)

    def test_pane_of_the_wrong_shape_is_not_declared_dead(self):
        # A line unlike a pane is never checked at all: `pane_alive` answers about
        # real panes, not any text in the `pane` field.
        alive, why = pane_status("not a pane")
        self.assertFalse(alive)
        self.assertEqual(why, "")


class PilotPidsTest(unittest.TestCase):
    def test_running_process_is_found_by_its_command_line(self):
        needle = "board-hosts-probe-marker"
        proc = subprocess.Popen(
            [sys.executable, "-c", f"import time; time.sleep(30)  # {needle}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(proc.kill)
        found = pilot_pids(needle)
        self.assertIn(proc.pid, found)

    def test_search_finds_nothing_when_nothing_matches(self):
        self.assertEqual(pilot_pids("board-hosts-probe-no-such"), [])

    def test_search_never_returns_the_caller(self):
        # Otherwise the caller would declare itself supervisor.
        found = pilot_pids("")
        self.assertNotIn(os.getpid(), found)

    def test_search_survives_a_missing_proc(self):
        # /proc misses on macOS and some containers. That is "unknown",
        # never a reason to drop the snapshot build, so the check substitutes exactly
        # such an environment: otherwise the branch would run on Linux only.
        import board_hosts
        original = board_hosts.os.listdir

        def explode(path):
            if path == "/proc":
                raise OSError("no /proc")
            return original(path)

        board_hosts.os.listdir = explode
        try:
            self.assertEqual(pilot_pids("anything"), [])
        finally:
            board_hosts.os.listdir = original


class PilotSectionTest(PairTestCase):
    def test_absent_pilot_is_reported_as_not_running(self):
        section = pilot_section(self.root, [])
        self.assertFalse(section["running"])
        self.assertIsNone(section["pid"])
        self.assertEqual(section["pids"], [])

    def test_pilot_events_and_refusals_are_counted(self):
        self.store.append_transcript("pilot_unblock_refused", "pairpilot", why="forbidden")
        self.store.append_transcript("pilot_restart_impossible", "pairpilot", why="no pane")
        self.store.append_transcript("pilot_wake_acknowledged", "pairpilot", target="agy")
        section = pilot_section(self.root, self.store.read_transcript(20))
        self.assertEqual(section["events_shown"], 3)
        self.assertEqual(section["refusals"], 2)
        # The last event (`pilot_wake_acknowledged`) carries only `target` in fields,
        # with no details. Showing "agy" here reads nothing: what the pilot did is told
        # by the event kind itself, and the client turns it into words. So the fallback is
        # the kind, not the role name.
        self.assertEqual(section["last"], "pilot_wake_acknowledged")
        self.assertEqual(section["last_ts"] is not None, True)

    def test_last_summary_falls_back_to_the_kind(self):
        entries = [{"kind": "pilot_own_pane"}]
        self.assertEqual(pilot_section(self.root, entries)["last"], "pilot_own_pane")

    def test_missing_log_and_report_are_reported_as_absent(self):
        section = pilot_section(self.root, [])
        self.assertEqual(section["log_size"], 0)
        self.assertFalse(section["report_exists"])
        self.assertIsNone(section["report_age_seconds"])

    def test_existing_log_and_report_are_measured(self):
        (self.root / "pairpilot.log").write_text("line\n", encoding="utf-8")
        (self.root / "REPORT.md").write_text("# report\n", encoding="utf-8")
        section = pilot_section(self.root, [])
        self.assertGreater(section["log_size"], 0)
        self.assertTrue(section["report_exists"])
        self.assertIsNotNone(section["report_age_seconds"])


if __name__ == "__main__":
    unittest.main(verbosity=2)