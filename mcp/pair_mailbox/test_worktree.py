"""Unit tests for the worktree.py module (Git Worktrees - S5)."""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

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
            # Setting up a git repo
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

            # The source worktree list
            wts = worktree.list_worktrees(tmp_path)
            self.assertEqual(len(wts), 1)

            # Creating a worktree for the feat-test branch. The visa is the
            # agreed isolation strategy of the round — without it the creation
            # is refused (round 147 slice C).
            visa = {"ok": True, "mode": "worktree", "worktrees": 1}
            res = worktree.setup_worktree(tmp_path, "feat-test", visa=visa)
            self.assertTrue(res.get("ok"))
            self.assertTrue(res.get("created"))
            wt_path = Path(res["worktree"])
            self.assertTrue(wt_path.exists())

            # A repeat setup_worktree call returns ok: True, created: False
            res2 = worktree.setup_worktree(tmp_path, "feat-test", visa=visa)
            self.assertTrue(res2.get("ok"))
            self.assertFalse(res2.get("created"))

            # Checking the list now holds 2 worktrees
            wts2 = worktree.list_worktrees(tmp_path)
            self.assertEqual(len(wts2), 2)

            # Removing the created worktree
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

            # Checking the cmd_worktree list call
            args = MagicMock()
            args.worktree_action = "list"
            args.json = True
            code = worktree.cmd_worktree(store, args)
            self.assertEqual(code, 0)

            # Checking the cmd_worktree prune call
            args.worktree_action = "prune"
            args.json = True
            code = worktree.cmd_worktree(store, args)
            self.assertEqual(code, 0)


class TestWorktreeNeedsAnAgreedStrategy(PairTestCase):
    """Round 147 slice C: a worktree is not created before the critic agrees.

    The backlog position (P1) says a worktree must not be created before the
    official plan-ack. The gate lives in `setup_worktree` itself, fail-closed:
    a caller that forgets to pass the visa gets a refusal, never a worktree
    nobody agreed to.
    """

    def setUp(self) -> None:
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        subprocess.run(["git", "init"], cwd=str(self.repo), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "pair@test.local"],
                       cwd=str(self.repo), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Pair Test"],
                       cwd=str(self.repo), check=True, capture_output=True)
        (self.repo / "README.md").write_text("initial commit\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=str(self.repo), check=True,
                       capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=str(self.repo), check=True,
                       capture_output=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()
        super().tearDown()

    def test_without_a_visa_the_worktree_is_refused(self) -> None:
        res = worktree.setup_worktree(self.repo, "feat-test")
        self.assertFalse(res["ok"])
        self.assertIn("refused", res["error"])
        self.assertFalse((self.repo / ".worktrees" / "feat-test").exists())

    def test_a_refused_visa_is_refused_with_its_reason(self) -> None:
        res = worktree.setup_worktree(
            self.repo, "feat-test",
            visa={"ok": False, "why": "the critic never signed the plan"})
        self.assertFalse(res["ok"])
        self.assertIn("the critic never signed the plan", res["error"])

    def test_an_in_place_strategy_authorises_no_worktree(self) -> None:
        """The plan said no worktree was needed — so none is created."""
        res = worktree.setup_worktree(
            self.repo, "feat-test", visa={"ok": True, "mode": "in-place"})
        self.assertFalse(res["ok"])
        self.assertIn("in-place", res["error"])

    def test_an_agreed_worktree_strategy_creates_it(self) -> None:
        res = worktree.setup_worktree(
            self.repo, "feat-test",
            visa={"ok": True, "mode": "worktree", "worktrees": 1})
        self.assertTrue(res["ok"], res.get("error"))
        self.assertTrue(res["created"])
        self.assertTrue(Path(res["worktree"]).exists())

    def test_the_bootstrap_path_needs_no_visa(self) -> None:
        """The launch environment of a pair: no round exists yet, so no visa can.

        The human asked for it on the command line. The round that starts
        inside still declares its own strategy in its plan.
        """
        res = worktree.bootstrap_worktree(self.repo, "room/launch")
        self.assertTrue(res["ok"], res.get("error"))
        self.assertTrue(res["created"])


class TestCleanupAfterMerge(PairTestCase):
    """Round 147 slice D: the worktree dies with the branch it held.

    The commits are in the trunk and the branch is gone (`--delete-branch`);
    the DIRECTORY that held it is left over. §19 makes the merged round branch
    die by construction, and this makes the directory die with it.
    """

    def _repo_with_worktree(self, tmp: Path) -> tuple[Path, Path]:
        repo = tmp / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
        subprocess.run(["git", "config", "user.email", "p@t.t"], cwd=str(repo), check=True)
        subprocess.run(["git", "config", "user.name", "P"], cwd=str(repo), check=True)
        (repo / "a.txt").write_text("x\n", encoding="utf-8")
        subprocess.run(["git", "add", "a.txt"], cwd=str(repo), check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=str(repo), check=True)
        wt = repo / ".worktrees" / "round-147"
        subprocess.run(["git", "worktree", "add", "-b", "round-147", str(wt)],
                       cwd=str(repo), check=True, capture_output=True)
        return repo, wt

    def test_the_worktree_of_the_merged_branch_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, wt = self._repo_with_worktree(Path(tmp))
            self.assertTrue(wt.exists())
            res = worktree.cleanup_worktree_for_branch(repo, "round-147")
            self.assertTrue(res["ok"], res["why"])
            self.assertTrue(res["removed"])
            self.assertFalse(wt.exists())

    def test_the_main_worktree_is_never_removed(self) -> None:
        """The repository itself is not garbage: `git worktree remove` on it
        would be a catastrophe, not a cleanup."""
        with tempfile.TemporaryDirectory() as tmp:
            repo, _ = self._repo_with_worktree(Path(tmp))
            res = worktree.cleanup_worktree_for_branch(repo, "master")
            self.assertTrue(res["ok"])
            self.assertFalse(res["removed"])
            self.assertTrue(repo.exists())

    def test_nothing_to_clean_is_not_a_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, _ = self._repo_with_worktree(Path(tmp))
            res = worktree.cleanup_worktree_for_branch(repo, "no-such-branch")
            self.assertTrue(res["ok"])
            self.assertFalse(res["removed"])
            self.assertIn("no worktree held", res["why"])

    def test_the_branch_comes_from_github_not_from_the_local_head(self) -> None:
        """The cleanup must target the MERGED branch, not the branch the merge
        ran from.

        The local HEAD is usually the trunk. Asking for the trunk's worktree
        would either do nothing (the right outcome by accident) or try to
        remove the repository itself (the wrong outcome). A cleanup that
        targets the wrong branch is worse than no cleanup, because it looks
        like it worked.
        """
        import review_gate
        with patch("review_gate.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0,
                                              stdout="round-146-supervisor-watchdog\n",
                                              stderr="")
            name = review_gate._merged_branch(Path("/tmp"), "95")
        self.assertEqual(name, "round-146-supervisor-watchdog")
        cmd = mock_run.call_args[0][0]
        self.assertIn("headRefName", " ".join(cmd))

    def test_an_unreadable_pr_leaves_the_branch_unknown(self) -> None:
        """An empty answer means "unknown", never "no worktree" — and never
        the local HEAD as a substitute."""
        import review_gate
        with patch("review_gate.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="boom")
            name = review_gate._merged_branch(Path("/tmp"), "95")
        self.assertEqual(name, "")


if __name__ == "__main__":
    unittest.main()
