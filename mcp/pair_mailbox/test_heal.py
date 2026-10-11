"""Тесты для pair-heal (S4): авто-реопен done -> implement при evidence(ok=false).

Один тест-файл на один исходник: `heal.py` -> `test_heal.py`.
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
        # Запись сбойного evidence в implement не должна вызывать heal-reopen
        res = self.writer.tool_pair_evidence(kind="test", value="test failed", ok=False)
        self.assertEqual(self.store.read()["phase"], "implement")
        self.assertEqual(self.store.read()["round"], 1)

    def test_heal_triggers_on_failing_evidence_in_done(self) -> None:
        self.advance_phases(("plan", "implement", "verify"))
        self.writer.tool_pair_evidence(kind="test", value="tests ok", ok=True)
        self.writer.tool_pair_advance("done")
        self.assertEqual(self.store.read()["phase"], "done")
        self.assertEqual(self.store.read()["round"], 1)

        # Теперь критик или монитор фиксирует падение в done:
        self.peer.tool_pair_evidence(kind="test", value="regression detected", ok=False)

        state = self.store.read()
        # Фаза должна автоматически переоткрыться в implement, round увеличиться на 1:
        self.assertEqual(state["phase"], "implement")
        self.assertEqual(state["round"], 2)
        self.assertEqual(state["reopened_by"], "heal:reviewer-agent")

        # Должна появиться открытая задача auto-heal:
        tasks = state.get("tasks", [])
        heal_tasks = [t for t in tasks if "auto-heal" in t.get("text", "")]
        self.assertEqual(len(heal_tasks), 1)
        self.assertEqual(heal_tasks[0]["status"], "open")

    def test_heal_attempts_limit_prevents_loop(self) -> None:
        # Установим 3 попытки в heal-state.json
        heal.save_heal_state(self.store.root, {"attempts": 3, "last_heal": None})

        self.advance_phases(("plan", "implement", "verify"))
        self.writer.tool_pair_evidence(kind="test", value="tests ok", ok=True)
        self.writer.tool_pair_advance("done")

        # При превышении лимита фаза остается done
        self.peer.tool_pair_evidence(kind="test", value="regression again", ok=False)
        state = self.store.read()
        self.assertEqual(state["phase"], "done")
        self.assertEqual(state["round"], 1)


if __name__ == "__main__":
    unittest.main()
