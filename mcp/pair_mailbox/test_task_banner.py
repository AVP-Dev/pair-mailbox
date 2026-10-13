"""Баннер задачи раунда: то, что агенты видят в панели до первого хода.

Один тест-файл на один исходник: `task_banner.py` -> `test_task_banner.py`
(тот же порядок, что у obligations/review/roster_heal). Поведение через
инструмент пары, а не только через render: баннер обязан появляться от
настоящего `pair_task`, иначе он проекция теста, а не состояния.
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
        """Замер 2026-10-11: задача раунда жила только в state.json, и критика
        в новой сессии оставался без входного события. Баннер пишется от
        настоящего pair_task и печатается в обе панели pairup."""
        added = self.writer.tool_pair_task("add", text="починить панели")
        banner = self.banner()
        self.assertIn("ROUND TASK", banner)
        self.assertIn(f"#{added['task']['id']}", banner)
        self.assertIn("починить панели", banner)
        self.assertIn("pair_digest", banner)

    def test_empties_when_nothing_is_open(self) -> None:
        added = self.writer.tool_pair_task("add", text="задача А")
        self.writer.tool_pair_task("done", added["task"]["id"])
        self.assertEqual("", self.banner())

    def test_dropped_tasks_do_not_linger(self) -> None:
        added = self.writer.tool_pair_task("add", text="оказалось не нужно")
        self.writer.tool_pair_task("drop", added["task"]["id"], reason="дубль")
        self.assertEqual("", self.banner())

    def test_critic_added_task_reaches_the_banner(self) -> None:
        """Критик заводит задачу через тот же инструмент — баннер обязан
        увидеть её без второго механизма."""
        self.peer.tool_pair_task("add", text="ещё дыра: pane_alive")
        self.assertIn("pane_alive", self.banner())

    def test_render_puts_no_task_without_open_ones(self) -> None:
        self.assertEqual("", render_task_banner({"tasks": []}))
        self.assertEqual("", render_task_banner({"tasks": [{"id": 1, "status": "done"}]}))

    def test_write_survives_a_directory_that_disappeared(self) -> None:
        """Панель человека не должна ронять добавление задачи: отсутствие
        баннера деградирует к прежнему поведению, а не к отказу."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".pair"
            root.mkdir()
            write_task_banner(root, {"tasks": []})
            target = root / "task-banner.txt"
            target.write_text("x", encoding="utf-8")
            import os
            os.chmod(target, 0o000)
            try:
                write_task_banner(root, {"tasks": []})  # не бросает
            finally:
                os.chmod(target, 0o600)


if __name__ == "__main__":
    unittest.main()
