#!/usr/bin/env python3
"""The facts anchor: what survives a reset, in a shape an agent can read.

The prose it replaces cost 39 513 to 49 131 tokens per compaction and could not be
queried, diffed or dropped. A fact costs a few tokens, and the store has one rule
that matters more than its size: a write that would cross the budget is REFUSED,
never trimmed. A silently trimmed fact is a fact the writer believes they recorded
and nobody else can find.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import facts  # noqa: E402
from pair_test_base import PairTestCase  # noqa: E402
from server import ProtocolError  # noqa: E402


class TestFactsStore(PairTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.writer.tool_pair_claim(reason="writer")

    def test_a_fact_is_read_back(self) -> None:
        self.writer.tool_pair_facts(action="set", key="blocker", value="the gate reads a live pane")
        answer = self.writer.tool_pair_facts(action="list")
        self.assertEqual(1, answer["count"])
        self.assertIn("blocker", answer["text"])
        self.assertIn("the gate reads a live pane", answer["text"])
        self.assertLessEqual(answer["tokens"], answer["budget"])

    def test_the_same_key_is_replaced_not_stacked(self) -> None:
        self.writer.tool_pair_facts(action="set", key="blocker", value="first")
        self.writer.tool_pair_facts(action="set", key="blocker", value="second")
        answer = self.writer.tool_pair_facts(action="list")
        self.assertEqual(1, answer["count"])
        self.assertIn("second", answer["text"])
        self.assertNotIn("first", answer["text"])

    def test_the_store_refuses_a_write_over_the_budget(self) -> None:
        """Refused, not trimmed: a trimmed fact is a fact nobody can find."""
        refused = False
        for index in range(20):
            try:
                self.writer.tool_pair_facts(action="set", key=f"fact-{index}",
                                            value="y" * facts.VALUE_MAX)
            except ProtocolError:
                refused = True
                break
        self.assertTrue(refused, "the store never refused: the budget does not hold")
        answer = self.writer.tool_pair_facts(action="list")
        self.assertLessEqual(answer["tokens"], answer["budget"])
        self.assertNotIn("one-too-many", answer["text"])

    def test_the_critic_may_set_a_fact_and_the_writer_prunes_it(self) -> None:
        self.peer.tool_pair_facts(action="set", key="seen", value="the critic saw it")
        self.assertRefused(self.peer.tool_pair_facts, "drop", "seen",
                           contains="only the writer")
        self.writer.tool_pair_facts(action="drop", key="seen")
        self.assertEqual(0, self.writer.tool_pair_facts(action="list")["count"])

    def test_a_fact_survives_a_new_session_on_the_same_state(self) -> None:
        """The point of the anchor: it outlives the session that wrote it."""
        self.writer.tool_pair_facts(action="set", key="branch", value="round-135")
        from server import PairSession
        later = PairSession("writer-agent", self.store)
        answer = later.tool_pair_facts(action="list")
        self.assertEqual(1, answer["count"])
        self.assertIn("round-135", answer["text"])

    def test_the_digest_carries_the_facts(self) -> None:
        """The cheap orientation must show the anchor, or nobody reads it."""
        self.writer.tool_pair_facts(action="set", key="blocker", value="waiting for the verdict")
        text = self.writer.tool_pair_digest()["digest"]
        self.assertIn("blocker", text)
        self.assertIn("waiting for the verdict", text)

    def test_bad_arguments_are_refused(self) -> None:
        self.assertRefused(self.writer.tool_pair_facts, "set", key="../escape", value="x")
        self.assertRefused(self.writer.tool_pair_facts, "set", key="empty", value="")
        self.assertRefused(self.writer.tool_pair_facts, "set", key="long", value="x" * 500)
        self.assertRefused(self.writer.tool_pair_facts, "rewrite-everything")


if __name__ == "__main__":
    unittest.main()
