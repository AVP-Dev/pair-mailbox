"""Tests for pair-heal (S4): auto-reopen done -> implement on evidence(ok=false).

One test file per source: `heal.py` -> `test_heal.py`.
"""
from __future__ import annotations

from pathlib import Path
import unittest

from pair_test_base import PairTestCase
import heal
from server import ProtocolError


class TestPairHeal(PairTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.writer.tool_pair_claim(reason="writer")

    def test_heal_does_not_trigger_outside_done(self) -> None:
        self.advance_phases(("plan", "implement"))
        # A failing evidence write in implement must never trigger heal-reopen
        res = self.writer.tool_pair_evidence(kind="test", value="test failed", ok=False)
        self.assertEqual(self.store.read()["phase"], "implement")
        self.assertEqual(self.store.read()["round"], 1)

    def test_heal_triggers_on_failing_evidence_in_done(self) -> None:
        self.advance_phases(("plan", "implement", "verify"))
        self.record_test_evidence("tests ok")
        self.writer.tool_pair_advance("done")
        self.assertEqual(self.store.read()["phase"], "done")
        self.assertEqual(self.store.read()["round"], 1)

        # Now the critic or the monitor records the fall in done:
        self.peer.tool_pair_evidence(kind="test", value="regression detected", ok=False)

        state = self.store.read()
        # The phase must auto-reopen into implement, round growing by 1:
        self.assertEqual(state["phase"], "implement")
        self.assertEqual(state["round"], 2)
        self.assertEqual(state["reopened_by"], "heal:reviewer-agent")

        # An open auto-heal task must appear:
        tasks = state.get("tasks", [])
        heal_tasks = [t for t in tasks if "auto-heal" in t.get("text", "")]
        self.assertEqual(len(heal_tasks), 1)
        self.assertEqual(heal_tasks[0]["status"], "open")

    def test_heal_attempts_limit_prevents_loop(self) -> None:
        # Setting 3 attempts in heal-state.json
        heal.save_heal_state(self.store.root, {"attempts": 3, "last_heal": None})

        self.advance_phases(("plan", "implement", "verify"))
        self.record_test_evidence("tests ok")
        self.writer.tool_pair_advance("done")

        # Past the limit the phase stays done
        self.peer.tool_pair_evidence(kind="test", value="regression again", ok=False)
        state = self.store.read()
        self.assertEqual(state["phase"], "done")
        self.assertEqual(state["round"], 1)


if __name__ == "__main__":
    unittest.main()
