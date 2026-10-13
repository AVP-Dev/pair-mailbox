"""The round task banner: what agents see in the pane before the first move.

One test file per source: `task_banner.py` -> `test_task_banner.py`
(same order as obligations/review/roster_heal). Behaviour through the pair
tool, not only through render: the banner must appear off a real `pair_task`,
or it projects the test, not the state.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pair_test_base import PairTestCase
from task_banner import render_task_banner, write_task_banner


class TestTaskBanner(PairTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.writer.tool_pair_claim(reason="writer")

    def banner(self) -> str:
        return (self.store.root / "task-banner.txt").read_text(encoding="utf-8")

    def test_shows_open_tasks_from_the_real_tool(self) -> None:
        """Measured 2026-10-11: the round task lived only in state.json, and the critic
        in a new session stayed with no incoming event. The banner writes off a
        real pair_task and prints into both pairup panes."""
        added = self.writer.tool_pair_task("add", text="fix the panes")
        banner = self.banner()
        self.assertIn("ROUND TASK", banner)
        self.assertIn(f"#{added['task']['id']}", banner)
        self.assertIn("fix the panes", banner)
        self.assertIn("pair_digest", banner)

    def test_empties_when_nothing_is_open(self) -> None:
        added = self.writer.tool_pair_task("add", text="task A")
        self.writer.tool_pair_task("done", added["task"]["id"])
        self.assertEqual("", self.banner())

    def test_dropped_tasks_do_not_linger(self) -> None:
        added = self.writer.tool_pair_task("add", text="turned out unneeded")
        self.writer.tool_pair_task("drop", added["task"]["id"], reason="double")
        self.assertEqual("", self.banner())

    def test_critic_added_task_reaches_the_banner(self) -> None:
        """The critic raises a task through the same tool — the banner must see it
        with no second mechanism."""
        self.peer.tool_pair_task("add", text="another hole: pane_alive")
        self.assertIn("pane_alive", self.banner())

    def test_render_puts_no_task_without_open_ones(self) -> None:
        self.assertEqual("", render_task_banner({"tasks": []}))
        self.assertEqual("", render_task_banner({"tasks": [{"id": 1, "status": "done"}]}))

    def test_write_survives_a_directory_that_disappeared(self) -> None:
        """The human pane must not drop a task add: a missing banner degrades to the
        old behaviour, not to a refusal."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".pair"
            root.mkdir()
            write_task_banner(root, {"tasks": []})
            target = root / "task-banner.txt"
            target.write_text("x", encoding="utf-8")
            import os
            os.chmod(target, 0o000)
            try:
                write_task_banner(root, {"tasks": []})  # never throws
            finally:
                os.chmod(target, 0o600)


if __name__ == "__main__":
    unittest.main()
