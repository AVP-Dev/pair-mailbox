"""The guard: one rule must never take the whole pass down with it.

The measured death (2026-10-25, this project's own supervisor): one
`tmux send-keys` into a pane whose CLI had stopped consuming input hung for
the full 5-second timeout and raised `subprocess.TimeoutExpired`. The pilot's
`flush_outbox` caught `ComposerBusy` and nothing else, so the exception
escaped the tick, the whole `pairpilot` died, and EVERY rule it carried went
dark with it — the silence check, the stalled-agent check, and the monologue
watchdog. A dead supervisor is the one failure no watchdog can notice.

So the guard is a unit of its own, and this file is its test.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from guard import guarded, run_rules  # noqa: E402


class TestGuardedTurnsAFailureIntoARecord(unittest.TestCase):
    """One failing call is a record, not a death."""

    def test_a_raising_call_does_not_escape(self) -> None:
        logs: list[str] = []
        journal: list[tuple] = []

        def boom() -> None:
            raise RuntimeError("tmux hung")

        result = guarded(boom, logs.append, journal=lambda kind, **f: journal.append((kind, f)))

        self.assertIsNone(result, "a failed call has no value")
        self.assertTrue(logs, "the failure must be visible in the log")
        self.assertTrue(journal, "the failure must be journalled")
        self.assertEqual("pilot_call_failed", journal[0][0])

    def test_the_value_passes_through(self) -> None:
        self.assertEqual(42, guarded(lambda: 42, lambda _m: None))

    def test_a_broken_journal_does_not_kill_the_pass(self) -> None:
        """The journal is a side effect, not the guard itself."""
        logs: list[str] = []

        def broken_journal(kind, **fields) -> None:
            raise RuntimeError("journal is broken too")

        def boom() -> None:
            raise RuntimeError("the rule failed")

        result = guarded(boom, logs.append, journal=broken_journal)

        self.assertIsNone(result)
        self.assertTrue(logs, "the log still carries the failure")


class TestRunRulesKeepsGoing(unittest.TestCase):
    """The supervisor's value is the SUM of its rules."""

    def test_a_failing_rule_does_not_stop_the_ones_after_it(self) -> None:
        ran: list[str] = []
        logs: list[str] = []
        journal: list[tuple] = []

        def first(state: dict) -> None:
            ran.append("first")

        def boom(state: dict) -> None:
            ran.append("boom")
            raise RuntimeError("rule exploded")

        def last(state: dict) -> None:
            ran.append("last")

        failed = run_rules((first, boom, last), {}, logs.append,
                           journal=lambda kind, **f: journal.append((kind, f)))

        self.assertEqual(["first", "boom", "last"], ran,
                         "the rules after the failure must still run")
        self.assertEqual(["boom"], failed)
        self.assertTrue(journal, "the failing rule must be journalled")
        self.assertEqual("pilot_rule_failed", journal[0][0])
        self.assertEqual("boom", journal[0][1].get("rule"))

    def test_every_rule_failing_still_returns_the_names(self) -> None:
        def boom(state: dict) -> None:
            raise RuntimeError("nope")

        failed = run_rules((boom, boom), {}, lambda _m: None)
        self.assertEqual(["boom", "boom"], failed)

    def test_a_passing_pass_returns_no_failures(self) -> None:
        failed = run_rules((lambda s: None, lambda s: None), {}, lambda _m: None)
        self.assertEqual([], failed)

    def test_the_state_reaches_every_rule(self) -> None:
        seen: list[dict] = []
        state = {"phase": "verify"}
        run_rules((lambda s: seen.append(s),), state, lambda _m: None)
        self.assertEqual([state], seen)


if __name__ == "__main__":
    unittest.main(verbosity=2)
