"""Unit-тесты для модуля worktree.py (Git Worktrees — S5)."""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from pair_test_base import PairTestCase
import worktree


class TestWorktree(PairTestCase):
    def test_list_worktrees_empty_or_non_git(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            res = worktree.list_worktrees(Path(tmp))
            self.assertEqual(res, [])

    def test_setup_and_list_and_remove_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            # Инициализируем git репозиторий
            subprocess.run(["git", "init"], cwd=str(tmp_path), check=True, capture_output=True)
            subprocess.run(
                ["git", "config", "user.email", "pair@test.local"],
                cwd=str(tmp_path),
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Pair Test"],
                cwd=str(tmp_path),
                check=True,
                capture_output=True,
            )
            readme = tmp_path / "README.md"
            readme.write_text("initial commit\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=str(tmp_path), check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=str(tmp_path), check=True, capture_output=True)

            # Список исходных worktree
            wts = worktree.list_worktrees(tmp_path)
            self.assertEqual(len(wts), 1)

            # Создаем worktree для ветки feat-test
            res = worktree.setup_worktree(tmp_path, "feat-test")
            self.assertTrue(res.get("ok"))
            self.assertTrue(res.get("created"))
            wt_path = Path(res["worktree"])
            self.assertTrue(wt_path.exists())

            # Повторный вызов setup_worktree возвращает ok: True, created: False
            res2 = worktree.setup_worktree(tmp_path, "feat-test")
            self.assertTrue(res2.get("ok"))
            self.assertFalse(res2.get("created"))

            # Проверяем, что в списке теперь 2 worktrees
            wts2 = worktree.list_worktrees(tmp_path)
            self.assertEqual(len(wts2), 2)

            # Удаляем созданный worktree
            ok = worktree.remove_worktree(tmp_path, wt_path)
            self.assertTrue(ok)

            # Prune
            prune_ok = worktree.prune_worktrees(tmp_path)
            self.assertTrue(prune_ok)

    def test_cmd_worktree_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            subprocess.run(["git", "init"], cwd=str(tmp_path), check=True, capture_output=True)
            store = MagicMock()
            store.root = tmp_path / ".pair"

            # Проверяем вызов cmd_worktree list
            args = MagicMock()
            args.worktree_action = "list"
            args.json = True
            code = worktree.cmd_worktree(store, args)
            self.assertEqual(code, 0)

            # Проверяем вызов cmd_worktree prune
            args.worktree_action = "prune"
            args.json = True
            code = worktree.cmd_worktree(store, args)
            self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
