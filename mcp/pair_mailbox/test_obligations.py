#!/usr/bin/env python3
"""Reply-obligation tests. Matching `obligations.py`.

Moved out of `test_protocol.py` (the server-py-still-a-monolith card): request, reply
and verdict-obligation checks live beside the module they check, not in a shared
4000-line file.

Run:  python3 mcp/pair_mailbox/test_obligations.py
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
    """A request awaiting an answer must show as an obligation. Or it dies in the box,
    and the pair reads calm exactly till morning."""

    def test_small_talk_creates_no_obligation(self) -> None:
        self.writer.tool_pair_send(to="reviewer-agent", text="hello")
        self.assertEqual([], list(self.store.obligations_root.glob("*.json")))

    def test_request_creates_an_obligation(self) -> None:
        result = self.writer.tool_pair_send(
            to="reviewer-agent", text="check that the linter is green", expects_reply=True)
        self.assertTrue(result["awaits_answer"])
        files = list(self.store.obligations_root.glob("*.json"))
        self.assertEqual(1, len(files))
        record = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual("writer-agent", record["from"])
        self.assertEqual("reviewer-agent", record["to"])
        self.assertEqual(result["seq"], record["seq"])

    def test_reply_closes_the_obligation(self) -> None:
        self.writer.tool_pair_send(to="reviewer-agent", text="check it", expects_reply=True)
        self.peer.tool_pair_send(to="writer-agent", text="checked, green")
        self.assertEqual([], list(self.store.obligations_root.glob("*.json")))

    def test_reply_from_a_third_role_does_not_close_it(self) -> None:
        self.writer.tool_pair_send(to="reviewer-agent", text="check it", expects_reply=True)
        outsider = PairSession("codex-agent", self.store)
        outsider.tool_pair_send(to="writer-agent", text="nothing to do with me")
        self.assertEqual(1, len(list(self.store.obligations_root.glob("*.json"))))

    def test_answering_twice_is_not_an_error(self) -> None:
        self.writer.tool_pair_send(to="reviewer-agent", text="check it", expects_reply=True)
        self.peer.tool_pair_send(to="writer-agent", text="first answer")
        self.peer.tool_pair_send(to="writer-agent", text="plus a clarification")
        self.assertEqual([], list(self.store.obligations_root.glob("*.json")))

    def test_verdict_opens_obligation_for_writer(self) -> None:
        """A verdict that still owes work opens an obligation (the full cycle).

        Measured 2026-10-06: verdict seq 252 lay 3.5 hours, the writer stood on a
        "awaiting the verdict" recap, the pilot stayed silent — nothing to watch,
        no obligation file. Now the pilot pokes the recipient automatically.
        """
        self.writer.tool_pair_claim(reason="taking the pen")
        self.peer.tool_pair_note(kind="verdict", text="needs work: the why still collapses")
        files = list(self.store.obligations_root.glob("*.json"))
        self.assertEqual(1, len(files))
        record = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual("reviewer-agent", record["from"])
        self.assertEqual("writer-agent", record["to"])
        self.assertIn("verdict", record.get("subject", ""))

    def test_a_final_approve_opens_no_obligation(self) -> None:
        """Round 104 S3: a verdict that says "fine as is" owes nobody an answer.

        This test used to write `text="accepted, closing"` and assert an
        obligation — the exact shape peer's live case proved wrong: the approve
        left pending_reviews=1 and the round could not close over its own
        finished review. An approve is a report, not a request.
        """
        self.writer.tool_pair_claim(reason="taking the pen")
        self.peer.tool_pair_note(kind="verdict", text="accepted, closing")
        self.assertEqual([], list(self.store.obligations_root.glob("*.json")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
