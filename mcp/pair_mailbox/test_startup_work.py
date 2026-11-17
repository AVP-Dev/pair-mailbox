"""Tests for the pre-launch resume inventory and task selection."""
from __future__ import annotations

import io
import subprocess
import tempfile
import unittest
from pathlib import Path

import startup_work


class StartupWorkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "project"
        self.root.mkdir()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(["git", "config", "user.email", "pair@test.local"],
                       cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.name", "Pair Test"],
                       cwd=self.root, check=True)
        (self.root / "README.md").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "initial"], cwd=self.root, check=True)

    def state(self, tasks: list[dict] | None = None) -> dict:
        return {"round": 7, "tasks": tasks or []}

    def dirty_worktree(self) -> Path:
        target = self.root / ".worktrees" / "resume-work"
        target.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "worktree", "add", "-q", "-b", "resume-work",
                        str(target), "HEAD"], cwd=self.root, check=True)
        (target / "README.md").write_text("unfinished Work3 changes\n", encoding="utf-8")
        (target / "new-file.py").write_text("unfinished\n", encoding="utf-8")
        return target

    def test_closed_work3_with_dirty_worktree_is_a_resume_candidate(self) -> None:
        target = self.dirty_worktree()
        state = self.state([{"id": 20, "text": "Work3 coordinator", "status": "done",
                             "round": 7}])

        candidates = startup_work.build_candidates(state, self.root)
        work3 = next(item for item in candidates if item.get("task_id") == 20)

        self.assertEqual("done", work3["status"])
        self.assertEqual(str(target.resolve()), work3["workspace"])
        self.assertEqual(["README.md", "new-file.py"], work3["files"])

    def test_selecting_closed_task_preserves_its_status_and_paths(self) -> None:
        target = self.dirty_worktree()
        state = self.state([{"id": 20, "text": "Work3 coordinator", "status": "done",
                             "round": 7}])

        selected = startup_work.choose_work(
            state, self.root, resume="20", interactive=False,
            stderr=io.StringIO(),
        )

        self.assertEqual("resume", selected["action"])
        self.assertEqual(20, selected["task_id"])
        self.assertEqual("done", selected["status"])
        self.assertEqual(str(target.resolve()), selected["workspace"])
        self.assertEqual("done", state["tasks"][0]["status"])

    def test_terminal_can_select_closed_work3_from_the_displayed_list(self) -> None:
        target = self.dirty_worktree()
        state = self.state([{"id": 20, "text": "Work3 coordinator", "status": "done",
                             "round": 7}])
        output = io.StringIO()

        selected = startup_work.choose_work(
            state, self.root, interactive=True, stdin=io.StringIO("1\n"), stderr=output,
        )

        self.assertEqual("resume", selected["action"])
        self.assertEqual(20, selected["task_id"])
        self.assertEqual("done", selected["status"])
        self.assertEqual(str(target.resolve()), selected["workspace"])
        self.assertIn("#20 [done] Work3 coordinator", output.getvalue())
        self.assertIn("changed files:", output.getvalue())
        self.assertIn("new-file.py", output.getvalue())

    def test_terminal_can_choose_new_task_from_the_candidate_menu(self) -> None:
        state = self.state([{"id": 3, "text": "existing task", "status": "open",
                             "round": 7}])
        terminal = io.StringIO("2\nwrite documentation\n")
        output = io.StringIO()

        selected = startup_work.choose_work(
            state, self.root, interactive=True, stdin=terminal, stderr=output,
        )

        self.assertEqual({"action": "new", "task": "write documentation"}, selected)
        self.assertIn("#3 [open] existing task", output.getvalue())
        self.assertIn("Start a new task", output.getvalue())

    def test_no_candidates_does_not_invent_a_resume_item(self) -> None:
        selected = startup_work.choose_work(self.state(), self.root, interactive=False,
                                            stderr=io.StringIO())
        self.assertEqual({"action": "none"}, selected)

    def test_noninteractive_launch_refuses_to_guess_between_open_tasks(self) -> None:
        state = self.state([{"id": 4, "text": "continue me", "status": "open",
                             "round": 7},
                            {"id": 5, "text": "other task", "status": "open",
                             "round": 7}])
        with self.assertRaisesRegex(ValueError, "needs a choice"):
            startup_work.choose_work(state, self.root, interactive=False,
                                     stderr=io.StringIO())

    def test_terminal_inventory_escapes_control_characters_from_git_paths(self) -> None:
        target = self.dirty_worktree()
        hostile_name = "line\nbreak-\x1b[31mred.py"
        (target / hostile_name).write_text("untrusted path\n", encoding="utf-8")
        state = self.state([{"id": 20, "text": "Work3 coordinator", "status": "done",
                             "round": 7}])
        output = io.StringIO()

        startup_work.choose_work(state, self.root, interactive=True,
                                 stdin=io.StringIO("1\n"), stderr=output)

        self.assertNotIn("\x1b", output.getvalue())
        self.assertNotIn("line\nbreak", output.getvalue())
        self.assertIn(r"line\x0abreak-\x1b[31mred.py", output.getvalue())


if __name__ == "__main__":
    unittest.main()
