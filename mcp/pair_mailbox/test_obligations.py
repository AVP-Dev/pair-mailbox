#!/usr/bin/env python3
"""Тесты обязательств ответа. Соответствуют `obligations.py`.

Вынесены из `test_protocol.py` (карточка server-py-still-a-monolith): проверки
запроса, ответа и вердикта-обязательства живут рядом с модулем, который они
проверяют, а не в общем файле на 4000 строк.

Запуск:  python3 mcp/pair_mailbox/test_obligations.py
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pair_test_base import PairTestCase  # noqa: E402
from server import PairSession  # noqa: E402


class TestObligations(PairTestCase):
    """Запрос, на который ждут ответа, должен быть виден как обязательство. Иначе
    он умирает в ящике, и пара выглядит спокойно ровно до утра."""

    def test_small_talk_creates_no_obligation(self) -> None:
        self.writer.tool_pair_send(to="reviewer-agent", text="привет")
        self.assertEqual([], list(self.store.obligations_root.glob("*.json")))

    def test_request_creates_an_obligation(self) -> None:
        result = self.writer.tool_pair_send(
            to="reviewer-agent", text="проверь, что линтер зелёный", expects_reply=True)
        self.assertTrue(result["awaits_answer"])
        files = list(self.store.obligations_root.glob("*.json"))
        self.assertEqual(1, len(files))
        record = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual("writer-agent", record["from"])
        self.assertEqual("reviewer-agent", record["to"])
        self.assertEqual(result["seq"], record["seq"])

    def test_reply_closes_the_obligation(self) -> None:
        self.writer.tool_pair_send(to="reviewer-agent", text="проверь", expects_reply=True)
        self.peer.tool_pair_send(to="writer-agent", text="проверил, зелено")
        self.assertEqual([], list(self.store.obligations_root.glob("*.json")))

    def test_reply_from_a_third_role_does_not_close_it(self) -> None:
        self.writer.tool_pair_send(to="reviewer-agent", text="проверь", expects_reply=True)
        outsider = PairSession("codex-agent", self.store)
        outsider.tool_pair_send(to="writer-agent", text="я тут ни при чём")
        self.assertEqual(1, len(list(self.store.obligations_root.glob("*.json"))))

    def test_answering_twice_is_not_an_error(self) -> None:
        self.writer.tool_pair_send(to="reviewer-agent", text="проверь", expects_reply=True)
        self.peer.tool_pair_send(to="writer-agent", text="первый ответ")
        self.peer.tool_pair_send(to="writer-agent", text="и ещё уточнение")
        self.assertEqual([], list(self.store.obligations_root.glob("*.json")))

    def test_verdict_opens_obligation_for_writer(self) -> None:
        """Вердикт критика открывает обязательство писателю (полный цикл).

        Измерено 2026-10-06: вердикт seq 252 лежал 3.5 часа, писатель стоял
        на recap «жду вердикт», пилот молчал — сторожить было нечего,
        obligation-файла не было. Теперь пилот пинает получателя автоматом.
        """
        self.writer.tool_pair_claim(reason="беру перо")
        self.peer.tool_pair_note(kind="verdict", text="принято, закрывай")
        files = list(self.store.obligations_root.glob("*.json"))
        self.assertEqual(1, len(files))
        record = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual("reviewer-agent", record["from"])
        self.assertEqual("writer-agent", record["to"])
        self.assertIn("вердикт", record.get("subject", ""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
