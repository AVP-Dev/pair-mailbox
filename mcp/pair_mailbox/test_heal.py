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

    def test_a_hand_reopen_ends_the_streak(self) -> None:
        """PROTOCOL §10f calls the cap CONSECUTIVE auto-heals.

        Before the fix the counter was never reset by anything, so the third
        failure of any kind over the project's whole life disabled
        self-healing for good — and the only way back was for a human to
        delete `.pair/heal-state.json` by hand.

        The streak ends when a ROLE reopens a closed round: that is the human
        or the peer taking the work back, and the next incident then starts
        from zero. The auto-heal path reopens without passing through that
        move, so a flapping loop still spends the cap — which is what the cap
        is for.
        """
        # Three failures, each followed by a hand reopen of the closed round.
        for incident in range(1, 4):
            self.advance_phases(("plan", "implement", "verify"))
            self.record_test_evidence(f"tests ok, incident {incident}")
            self.writer.tool_pair_advance("done")

            self.peer.tool_pair_evidence(
                kind="test", value=f"regression {incident}", ok=False)
            state = self.store.read()
            self.assertEqual(state["phase"], "implement",
                             f"incident {incident} did not self-heal")
            self.assertEqual(int(heal.load_heal_state(self.store.root).get("attempts", 0)), 1,
                             f"incident {incident} spent more than one attempt")

            # The human takes the work back: the healed round is closed by
            # hand, then reopened by hand — a move the auto-heal path never
            # makes.
            self._close_auto_heal_tasks()
            self.advance_phases(("verify",))
            self.record_test_evidence(f"tests ok again, incident {incident}")
            self.writer.tool_pair_advance("done")
            self.writer.tool_pair_advance("implement")
            self.assertEqual(
                int(heal.load_heal_state(self.store.root).get("attempts", 0)), 0,
                f"the streak survived the hand reopen (incident {incident})")

    def test_a_flapping_loop_still_spends_the_cap(self) -> None:
        """The other half of the contract: the cap must survive. The flapping
        case is heal → close → fail → heal, with nobody reopening by hand.

        A reset at the close would make this loop endless, which is exactly
        what the cap exists to stop.
        """
        self.advance_phases(("plan", "implement", "verify"))
        self.record_test_evidence("tests ok")
        self.writer.tool_pair_advance("done")

        healed = 0
        for _ in range(6):
            before = int(heal.load_heal_state(self.store.root).get("attempts", 0))
            self.peer.tool_pair_evidence(kind="test", value="regression", ok=False)
            after = int(heal.load_heal_state(self.store.root).get("attempts", 0))
            if after == before:
                # The cap refused: the phase must stay done.
                self.assertEqual(self.store.read()["phase"], "done")
                self.assertEqual(healed, 3,
                                 "the cap must stop the loop after three heals")
                return
            healed += 1
            # The pair works the healed round and closes it again, with no hand
            # reopen anywhere: the auto-heal path alone moves the phase.
            self._close_auto_heal_tasks()
            self.advance_phases(("verify",))
            self.record_test_evidence("tests ok")
            self.writer.tool_pair_advance("done")
        self.fail("the flapping loop never hit the cap")

    def _close_auto_heal_tasks(self) -> None:
        """Close the auto-heal tasks the heal opened, as the pair would."""
        for task in self.store.read().get("tasks", []):
            if "auto-heal" in task.get("text", "") and task.get("status") == "open":
                self.writer.tool_pair_task(action="done", id=int(task["id"]))

    def test_pairctl_reset_clears_the_counter(self) -> None:
        """A reset that leaves the counter behind resets nothing: the next
        incident would spend an attempt from a streak the human believes they
        cleared."""
        import argparse

        from pairctl_authority import cmd_reset

        heal.save_heal_state(self.store.root, {"attempts": 2, "last_heal": None})
        self.assertTrue((self.store.root / "heal-state.json").exists())

        cmd_reset(self.store, argparse.Namespace(yes=True))

        self.assertFalse((self.store.root / "heal-state.json").exists(),
                         "pairctl reset left the self-heal counter behind")
        self.assertEqual(int(heal.load_heal_state(self.store.root).get("attempts", 0)), 0)


if __name__ == "__main__":
    unittest.main()
