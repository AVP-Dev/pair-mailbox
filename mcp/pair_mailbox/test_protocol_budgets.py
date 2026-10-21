#!/usr/bin/env python3
"""The budgeted tools: the task list, the note index and the note tail.

Round 134 measured what one orientation costs in this pair, and the answer was
29 784 tokens for a list with one open task, 8 065 for an index of 229 notes, and
3 166 to 5 122 for the 120-line tail of a note. The properties below are the ones
the change bought:

- the default answer does not grow with the history behind it;
- the history stays reachable, unchanged, for the caller who asks for it;
- a tail costs no more than the budget says, whatever the note is written in;
- a cut lands on a record edge, never inside a record.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pair_test_base import PairTestCase  # noqa: E402
from server import ProtocolError, estimate_tokens  # noqa: E402


class TestTaskListWindow(PairTestCase):
    """The list answers with the open window; the history is behind a scope."""

    def setUp(self) -> None:
        super().setUp()
        self.writer.tool_pair_claim(reason="writer")

    def _seed_closed(self, count: int) -> None:
        def seed(state: dict) -> None:
            items = state.setdefault("tasks", [])
            for index in range(count):
                items.append({"id": len(items) + 1, "text": f"closed task {index}",
                              "status": "done", "added_at": "now", "added_by": "human",
                              "round": 1, "reason": "", "closed_at": "now",
                              "closed_by": "human"})
        self.store.mutate(seed)

    def test_the_default_list_answers_with_the_open_window(self) -> None:
        open_id = self.writer.tool_pair_task(action="add", text="still open")["task"]["id"]
        closed_id = self.writer.tool_pair_task(action="add", text="already closed")["task"]["id"]
        self.writer.tool_pair_task(action="done", id=closed_id)
        answer = self.writer.tool_pair_task(action="list")
        self.assertEqual([open_id], [item["id"] for item in answer["tasks"]])
        self.assertEqual({"open": 1, "done": 1, "dropped": 0, "total": 2}, answer["counts"])
        self.assertEqual(1, answer["hidden"])
        self.assertIn("history", answer["hint"])

    def test_the_history_is_reachable_and_unchanged(self) -> None:
        for index in range(5):
            self.writer.tool_pair_task(action="add", text=f"task {index}")
        history = self.writer.tool_pair_task(action="list", scope="history")
        self.assertEqual([item["id"] for item in self.store.read()["tasks"]],
                         [item["id"] for item in history["tasks"]],
                         "the history came back different: no loss, no duplication")
        self.assertEqual(5, history["counts"]["total"])
        self.assertEqual(0, history["hidden"])

    def test_the_history_pages(self) -> None:
        for index in range(5):
            self.writer.tool_pair_task(action="add", text=f"task {index}")
        page = self.writer.tool_pair_task(action="list", scope="history", limit=2, offset=1)
        self.assertEqual([2, 3], [item["id"] for item in page["tasks"]])
        self.assertEqual(3, page["hidden"])

    def test_a_thousand_closed_tasks_cost_the_default_answer_nothing(self) -> None:
        """The property being bought: the default answer does not grow with the history."""
        self._seed_closed(1000)
        self.writer.tool_pair_task(action="add", text="the only open one")
        answer = self.writer.tool_pair_task(action="list")
        payload = json.dumps(answer, ensure_ascii=False)
        self.assertLess(len(payload), 2000,
                        f"the open window grew with the history: {len(payload)} chars")
        self.assertLess(estimate_tokens(payload), 800)
        self.assertEqual(1, answer["counts"]["open"])
        self.assertEqual(1000, answer["counts"]["done"])

    def test_bad_scope_and_page_arguments_are_refused(self) -> None:
        self.assertRefused(self.writer.tool_pair_task, "list", scope="everything")
        self.assertRefused(self.writer.tool_pair_task, "list", limit=0)
        self.assertRefused(self.writer.tool_pair_task, "list", offset=-1)


class TestNoteIndexWindow(PairTestCase):
    """The index lists the freshest notes and the room's totals; the rest is a scope away."""

    def _seed(self, kind: str, entries: int = 3, body: str = "content line") -> Path:
        path = self.store.notes_root / f"{kind}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            for index in range(entries):
                handle.write(f"\n## 2026-10-05T10:{index:02d}:00+00:00 — agy\n\n{index} {body}\n")
        return path

    def test_the_index_lists_the_freshest_notes_with_the_totals(self) -> None:
        import os
        import time
        for index in range(14):
            self._seed(f"note-{index:02d}")
        now = time.time()
        os.utime(self.store.notes_root / "note-13.md", (now, now))
        os.utime(self.store.notes_root / "note-12.md", (now - 600, now - 600))
        answer = self.writer.tool_pair_note_index()
        kinds = [note["kind"] for note in answer["notes"]]
        self.assertEqual("note-13", kinds[0], "the freshest note comes first")
        self.assertEqual(12, len(kinds), "the default window is bounded")
        self.assertEqual(14, answer["count"], "the totals report the room, not the window")
        self.assertEqual(2, answer["omitted"])
        self.assertIsNotNone(answer["largest"])
        self.assertIn("bytes", answer["largest"])

    def test_the_index_can_be_asked_for_the_largest(self) -> None:
        self._seed("small", body="x")
        self._seed("big", entries=40, body="x" * 200)
        answer = self.writer.tool_pair_note_index(scope="largest")
        self.assertEqual("big", answer["notes"][0]["kind"])
        self.assertEqual(2, answer["count"])

    def test_a_hundred_notes_cost_the_default_answer_nothing(self) -> None:
        for index in range(100):
            self._seed(f"note-{index:03d}")
        answer = self.writer.tool_pair_note_index()
        payload = json.dumps(answer, ensure_ascii=False)
        self.assertLess(len(payload), 4000,
                        f"the index grew with the room: {len(payload)} chars")
        self.assertEqual(100, answer["count"])
        self.assertEqual(100 - len(answer["notes"]), answer["omitted"])

    def test_the_index_pages(self) -> None:
        for index in range(5):
            self._seed(f"note-{index}")
        page = self.writer.tool_pair_note_index(limit=2, offset=1)
        self.assertEqual(2, len(page["notes"]))
        self.assertEqual(3, page["omitted"])

    def test_bad_index_arguments_are_refused(self) -> None:
        self.assertRefused(self.writer.tool_pair_note_index, scope="everything")
        self.assertRefused(self.writer.tool_pair_note_index, limit=0)
        self.assertRefused(self.writer.tool_pair_note_index, offset=-1)


class TestNoteTailBudget(PairTestCase):
    """The tail costs what the budget says, whatever the note is written in."""

    def _seed(self, kind: str, entries: int = 5, body: str = "content line") -> Path:
        path = self.store.notes_root / f"{kind}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            for index in range(entries):
                handle.write(f"\n## 2026-10-05T10:{index:02d}:00+00:00 — agy\n\n{index} {body}\n")
        return path

    def test_a_note_of_long_lines_costs_no_more_than_the_budget(self) -> None:
        """The case the line budget could not hold: 120 long lines were 5 122 tokens."""
        self._seed("long", entries=60, body="y" * 400)
        answer = self.writer.tool_pair_note_read(kind="long")
        self.assertLessEqual(answer["tokens"], answer["token_budget"])
        self.assertEqual("tokens", answer["budget_applied"])
        self.assertTrue(answer["truncated"])

    def test_a_note_of_short_lines_costs_no_more_than_the_budget(self) -> None:
        self._seed("short", entries=200, body="y")
        answer = self.writer.tool_pair_note_read(kind="short")
        self.assertLessEqual(answer["tokens"], answer["token_budget"])

    def test_a_russian_note_costs_no_more_than_the_budget(self) -> None:
        """The room writes in Russian, and bytes/4 under-counted it by a third."""
        self._seed("ru", entries=60, body="быстрая лиса " * 30)
        answer = self.writer.tool_pair_note_read(kind="ru")
        self.assertLessEqual(answer["tokens"], answer["token_budget"])

    def test_the_cut_never_splits_a_record(self) -> None:
        self._seed("verdict", entries=40)
        whole = (self.store.notes_root / "verdict.md").read_text(encoding="utf-8")
        answer = self.writer.tool_pair_note_read(kind="verdict")
        lines = answer["text"].splitlines()
        self.assertTrue(lines)
        self.assertTrue(lines[0].startswith("## "), f"the tail started at {lines[0]!r}")
        self.assertIn(answer["text"], whole, "the tail is not a file suffix")

    def test_the_answer_says_which_budget_bound_it(self) -> None:
        """Which ceiling bound the answer is a fact the reader needs: a tail cut by
        lines and a tail cut by tokens cost different amounts."""
        self._seed("long", entries=60, body="y" * 400)
        self.assertEqual("tokens",
                         self.writer.tool_pair_note_read(kind="long")["budget_applied"])
        self._seed("short", entries=40)
        self.assertEqual("lines",
                         self.writer.tool_pair_note_read(kind="short", tail_lines=4,
                                                        token_budget=100000)["budget_applied"])
        self.assertEqual("none",
                         self.writer.tool_pair_note_read(kind="short", tail_lines=1000,
                                                        token_budget=100000)["budget_applied"])

    def test_the_line_window_still_works(self) -> None:
        self._seed("verdict", entries=30)
        answer = self.writer.tool_pair_note_read(kind="verdict", tail_lines=10)
        self.assertLessEqual(answer["returned_lines"], 12)
        self.assertEqual(10, answer["tail_lines"])
        by_limit = self.writer.tool_pair_note_read(kind="verdict", limit=10)
        self.assertEqual(by_limit["text"], answer["text"])

    def test_a_bigger_budget_buys_more_text(self) -> None:
        self._seed("verdict", entries=60, body="y" * 200)
        small = self.writer.tool_pair_note_read(kind="verdict", token_budget=200)
        large = self.writer.tool_pair_note_read(kind="verdict", token_budget=2000)
        self.assertLess(len(small["text"]), len(large["text"]))

    def test_bad_arguments_are_refused(self) -> None:
        self._seed("verdict", entries=2)
        self.assertRefused(self.writer.tool_pair_note_read, kind="../escape")
        self.assertRefused(self.writer.tool_pair_note_read, kind="verdict", tail_lines=0)
        self.assertRefused(self.writer.tool_pair_note_read, kind="verdict", token_budget=0)
        self.assertRefused(self.writer.tool_pair_note_read, kind="no-such")


if __name__ == "__main__":
    unittest.main()
