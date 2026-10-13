"""Тесты для intent_gate (S3) и Task Receipt (RECEIPT-<round>.md).

Один тест-файл на исходник: `intent_gate.py` -> `test_intent_gate.py`
"""
from __future__ import annotations

from pathlib import Path
import unittest

from pair_test_base import PairTestCase
import intent_gate
import receipt
from server import ProtocolError


def write_test_intent(state_dir: Path, round_number: int, sections: tuple = ("Problem", "Scope", "Outcome")) -> None:
    body = [f"round: {round_number}", ""]
    for s in sections:
        body += [f"## {s}", "описание", ""]
    (state_dir / intent_gate.INTENT_FILE).write_text("\n".join(body), encoding="utf-8")


class TestIntentGate(PairTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.writer.tool_pair_claim(reason="writer")

    def test_missing_intent_refuses_scoping_to_plan(self) -> None:
        (self.store.root / intent_gate.INTENT_FILE).unlink(missing_ok=True)
        with self.assertRaises(ProtocolError) as ctx:
            self.writer.tool_pair_advance("plan")
        self.assertIn("unreadable", str(ctx.exception))

    def test_wrong_round_refuses(self) -> None:
        write_test_intent(self.store.root, round_number=42)
        with self.assertRaises(ProtocolError) as ctx:
            self.writer.tool_pair_advance("plan")
        self.assertIn("round: 42", str(ctx.exception))

    def test_missing_sections_refuse(self) -> None:
        write_test_intent(self.store.root, round_number=1, sections=("Problem",))
        with self.assertRaises(ProtocolError) as ctx:
            self.writer.tool_pair_advance("plan")
        self.assertIn("misses sections", str(ctx.exception))

    def test_valid_intent_allows_scoping_to_plan(self) -> None:
        write_test_intent(self.store.root, round_number=1)
        res = self.writer.tool_pair_advance("plan")
        self.assertEqual(res["phase"], "plan")


class TestTaskReceipt(PairTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.writer.tool_pair_claim(reason="writer")

    def test_receipt_generated_on_done(self) -> None:
        # Пройдем фазы до verify
        self.advance_phases(("plan", "implement", "verify"))

        # Добавим задачу раунда и закроем ее
        self.writer.tool_pair_task(action="add", text="тестовая задача")
        self.writer.tool_pair_task(action="done", id=1)

        # Запишем доказательство
        self.record_test_evidence("tests ok")
        res = self.writer.tool_pair_advance("done")
        self.assertEqual(res["phase"], "done")

        # Проверяем наличие .pair/RECEIPT-1.md
        receipt_path = self.store.root / "RECEIPT-1.md"
        self.assertTrue(receipt_path.is_file())
        text = receipt_path.read_text(encoding="utf-8")
        self.assertIn("# Task Receipt — Round 1", text)
        self.assertIn("тестовая задача", text)
        self.assertIn("tests ok", text)
        self.assertIn("Budget Metrics", text)


if __name__ == "__main__":
    unittest.main()
