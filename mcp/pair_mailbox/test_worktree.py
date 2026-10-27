"""Unit tests for the worktree.py module (Git Worktrees - S5)."""
from __future__ import annotations

import os
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


class TestSharedCaches(PairTestCase):
    """Round 148 slice A: the caches are shared, and nothing else is.

    A worktree created from scratch has no `.venv` and no `node_modules`, so
    every worktree means a full reinstall. Sharing them by symlink is the
    difference between a second and a second-hour.
    """

    def _repo_with_caches(self, tmp: Path) -> tuple[Path, Path]:
        repo = tmp / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=str(repo), check=True)
        subprocess.run(["git", "config", "user.email", "p@t.t"], cwd=str(repo), check=True)
        subprocess.run(["git", "config", "user.name", "P"], cwd=str(repo), check=True)
        (repo / "a.txt").write_text("x\n", encoding="utf-8")
        subprocess.run(["git", "add", "a.txt"], cwd=str(repo), check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=str(repo), check=True)
        (repo / ".venv").mkdir()
        (repo / ".venv" / "marker").write_text("deps\n", encoding="utf-8")
        (repo / "node_modules").mkdir()
        (repo / "node_modules" / "pkg").mkdir()
        return repo, tmp / "wt"

    def test_the_shared_cache_is_linked_into_the_new_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, wt = self._repo_with_caches(Path(tmp))
            res = worktree.setup_worktree(repo, "feature", wt,
                                          visa={"ok": True, "mode": "worktree"})
            self.assertTrue(res["ok"], res.get("error"))
            self.assertTrue(wt.exists())
            link = wt / ".venv"
            self.assertTrue(link.is_symlink(), "the cache must be a symlink, not a copy")
            self.assertEqual((link / "marker").read_text(encoding="utf-8"), "deps\n")
            self.assertEqual((wt / "node_modules" / "pkg").is_dir(), True)

    def test_the_link_points_at_the_main_worktree_not_at_itself(self) -> None:
        """A worktree sharing its own dependency directory with itself would
        defeat the point of the isolation."""
        with tempfile.TemporaryDirectory() as tmp:
            repo, wt = self._repo_with_caches(Path(tmp))
            worktree.setup_worktree(repo, "feature", wt,
                                     visa={"ok": True, "mode": "worktree"})
            link = wt / ".venv"
            self.assertNotEqual(link.resolve(), link.parent.resolve())
            self.assertEqual(link.resolve(), (repo / ".venv").resolve())

    def test_an_existing_path_is_never_overwritten(self) -> None:
        """A link to a place that is not there is a broken link; overwriting a
        directory that is already there loses data."""
        with tempfile.TemporaryDirectory() as tmp:
            repo, wt = self._repo_with_caches(Path(tmp))
            wt.mkdir(parents=True)
            (wt / ".venv").mkdir()
            (wt / ".venv" / "mine").write_text("keep\n", encoding="utf-8")
            worktree.setup_worktree(repo, "feature", wt,
                                     visa={"ok": True, "mode": "worktree"})
            self.assertFalse((wt / ".venv").is_symlink(),
                             "an existing directory must not be replaced by a link")
            self.assertEqual((wt / ".venv" / "mine").read_text(encoding="utf-8"), "keep\n")

    def test_only_the_closed_list_is_shared(self) -> None:
        """A glob over the tree would share source files and tracked data
        along with the caches."""
        with tempfile.TemporaryDirectory() as tmp:
            repo, wt = self._repo_with_caches(Path(tmp))
            (repo / "src").mkdir()
            (repo / "src" / "app.py").write_text("code\n", encoding="utf-8")
            (repo / "secrets").mkdir()
            (repo / "secrets" / "token").write_text("s3cret\n", encoding="utf-8")
            worktree.setup_worktree(repo, "feature", wt,
                                     visa={"ok": True, "mode": "worktree"})
            self.assertFalse((wt / "src").exists(), "source must not be shared")
            self.assertFalse((wt / "secrets").exists(), "secrets must not be shared")

    def test_a_missing_source_leaves_no_broken_link(self) -> None:
        """A link to a place that is not there is a broken link."""
        with tempfile.TemporaryDirectory() as tmp:
            repo, wt = self._repo_with_caches(Path(tmp))
            # `.pytest_cache` is in the shared list but NOT in the source:
            # the project never ran pytest here.
            self.assertNotIn(".pytest_cache", [p.name for p in repo.iterdir()])
            worktree.setup_worktree(repo, "feature", wt,
                                     visa={"ok": True, "mode": "worktree"})
            self.assertFalse((wt / ".pytest_cache").is_symlink(),
                             "a cache absent from the source must not be linked")
            self.assertFalse((wt / ".pytest_cache").exists())

    def test_the_bootstrap_entry_point_shares_too(self) -> None:
        """The launch environment of a pair needs the dependencies as much as
        a task's worktree does."""
        with tempfile.TemporaryDirectory() as tmp:
            repo, wt = self._repo_with_caches(Path(tmp))
            res = worktree.bootstrap_worktree(repo, "room", wt)
            self.assertTrue(res["ok"], res.get("error"))
            self.assertTrue((wt / ".venv").is_symlink())

    def test_the_project_setting_overrides_the_default_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, _ = self._repo_with_caches(Path(tmp))
            (repo / ".pair").mkdir()
            (repo / ".pair" / "worktree-share").write_text(
                ".venv, custom-cache\n", encoding="utf-8")
            names = worktree.shared_paths(repo)
            self.assertEqual(names, [".venv", "custom-cache"])

    def test_the_environment_overrides_the_project_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, _ = self._repo_with_caches(Path(tmp))
            (repo / ".pair").mkdir()
            (repo / ".pair" / "worktree-share").write_text(".venv\n", encoding="utf-8")
            with patch.dict(os.environ, {"PAIR_WORKTREE_SHARE": "from-env"}):
                self.assertEqual(worktree.shared_paths(repo), ["from-env"])

    def test_the_default_list_is_used_when_the_project_says_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, _ = self._repo_with_caches(Path(tmp))
            self.assertEqual(worktree.shared_paths(repo), worktree.DEFAULT_SHARED)


class TestDurableJournal(PairTestCase):
    """Round 153: the worktree lifecycle trail that never rotates.

    The transcript rotates: four archives, the oldest deleted. A merge
    record that rides on it leaves the window, and a merge that left the
    window is indistinguishable from a merge that escaped the gate. The
    journal in `.pair/worktree-log.jsonl` is append-only and never rotated.
    """

    def test_the_journal_lives_in_the_state_dir_and_is_not_the_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / ".pair"
            path = worktree.worktree_journal_path(state)
            self.assertEqual(path.name, "worktree-log.jsonl")
            self.assertEqual(path.parent, state)

    def test_an_append_is_read_back_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / ".pair"
            worktree.worktree_journal_append(state, "created", branch="b")
            worktree.worktree_journal_append(state, "merged", branch="b", pr="7")
            rows = worktree.worktree_journal_read(state)
            self.assertEqual([r["event"] for r in rows], ["created", "merged"])
            self.assertEqual(rows[1]["pr"], "7")

    def test_an_unknown_event_is_refused(self) -> None:
        """A journal that accepts any word is a journal nobody can read."""
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / ".pair"
            with self.assertRaises(ValueError):
                worktree.worktree_journal_append(state, "escaped")

    def test_the_journal_is_append_only_and_never_rewritten(self) -> None:
        """A second append must not truncate the first: a journal rewritten
        whole loses everything the moment the write fails."""
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / ".pair"
            worktree.worktree_journal_append(state, "created", branch="b")
            first = worktree.worktree_journal_path(state).read_text(encoding="utf-8")
            worktree.worktree_journal_append(state, "removed", branch="b")
            second = worktree.worktree_journal_path(state).read_text(encoding="utf-8")
            self.assertTrue(second.startswith(first))

    def test_a_torn_line_does_not_hide_the_rest(self) -> None:
        """One unreadable line costs that line, not the trail."""
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / ".pair"
            worktree.worktree_journal_append(state, "created", branch="b")
            path = worktree.worktree_journal_path(state)
            with path.open("a", encoding="utf-8") as handle:
                handle.write("{torn\n")
            worktree.worktree_journal_append(state, "merged", branch="b")
            rows = worktree.worktree_journal_read(state)
            self.assertEqual([r["event"] for r in rows], ["created", "merged"])

    def test_an_empty_journal_reads_as_no_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(worktree.worktree_journal_read(Path(tmp) / ".pair"), [])

    def test_the_journal_survives_the_removal_of_the_worktree(self) -> None:
        """The whole point: `git worktree remove --force` deletes the
        directory, and the record of that worktree must not die with it."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
            subprocess.run(["git", "config", "user.email", "p@t.t"], cwd=str(repo), check=True)
            subprocess.run(["git", "config", "user.name", "P"], cwd=str(repo), check=True)
            (repo / "a.txt").write_text("x\n", encoding="utf-8")
            subprocess.run(["git", "add", "a.txt"], cwd=str(repo), check=True)
            subprocess.run(["git", "commit", "-qm", "init"], cwd=str(repo), check=True)
            wt = repo / ".worktrees" / "room"
            res = worktree.bootstrap_worktree(repo, "room", wt)
            self.assertTrue(res["ok"], res.get("error"))
            state = worktree.journal_state_dir(repo)
            self.assertTrue(worktree.worktree_journal_path(state).is_file())
            worktree.cleanup_worktree_for_branch(repo, "room")
            self.assertFalse(wt.exists())
            rows = worktree.worktree_journal_read(state)
            events = [r["event"] for r in rows]
            self.assertIn("created", events)
            self.assertIn("removed", events)

    def test_the_journal_goes_to_the_main_repository_not_the_worktree(self) -> None:
        """A linked worktree's `.pair` dies with the worktree; the trail of
        that worktree must live in the main checkout."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
            subprocess.run(["git", "config", "user.email", "p@t.t"], cwd=str(repo), check=True)
            subprocess.run(["git", "config", "user.name", "P"], cwd=str(repo), check=True)
            (repo / "a.txt").write_text("x\n", encoding="utf-8")
            subprocess.run(["git", "add", "a.txt"], cwd=str(repo), check=True)
            subprocess.run(["git", "commit", "-qm", "init"], cwd=str(repo), check=True)
            wt = repo / ".worktrees" / "room"
            worktree.bootstrap_worktree(repo, "room", wt)
            state = worktree.journal_state_dir(repo)
            self.assertEqual(state, repo / ".pair")

    def test_the_merge_journal_names_the_commits_it_carried(self) -> None:
        """The commits are captured BEFORE the branch dies: after the merge
        the shas are reachable only through the squash, which is a different
        sha."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
            subprocess.run(["git", "config", "user.email", "p@t.t"], cwd=str(repo), check=True)
            subprocess.run(["git", "config", "user.name", "P"], cwd=str(repo), check=True)
            (repo / "a.txt").write_text("x\n", encoding="utf-8")
            subprocess.run(["git", "add", "a.txt"], cwd=str(repo), check=True)
            subprocess.run(["git", "commit", "-qm", "init"], cwd=str(repo), check=True)
            base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo),
                                  capture_output=True, text=True).stdout.strip()
            wt = repo / ".worktrees" / "room"
            worktree.bootstrap_worktree(repo, "room", wt)
            (wt / "b.txt").write_text("y\n", encoding="utf-8")
            subprocess.run(["git", "add", "b.txt"], cwd=str(wt), check=True)
            subprocess.run(["git", "commit", "-qm", "work"], cwd=str(wt), check=True)
            sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(wt),
                                 capture_output=True, text=True).stdout.strip()
            state = worktree.journal_state_dir(repo)
            res = worktree.journal_merge(repo, "room", base, pr_id="12")
            self.assertEqual(res["commits"], [sha])
            rows = worktree.worktree_journal_read(state)
            committed = [r for r in rows if r["event"] == "committed"]
            merged = [r for r in rows if r["event"] == "merged"]
            self.assertEqual([r["sha"] for r in committed], [sha])
            self.assertEqual(merged[0]["pr"], "12")
            self.assertEqual(merged[0]["commits"], [sha])

    def test_the_merge_journal_uses_the_commits_the_caller_captured(self) -> None:
        """After the branch is deleted, reading it back finds nothing — so the
        caller's pre-merge capture must win."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            (repo / ".pair").mkdir()
            res = worktree.journal_merge(repo, "gone", "base", pr_id="3",
                                         commits=["deadbeef"])
            self.assertEqual(res["commits"], ["deadbeef"])

    def test_the_default_branch_is_stable_and_unique(self) -> None:
        """The same project always lands in the same worktree; two projects
        never share a branch."""
        with tempfile.TemporaryDirectory() as tmp:
            one = Path(tmp) / "one"
            two = Path(tmp) / "two"
            one.mkdir()
            two.mkdir()
            first = worktree.default_branch(one)
            self.assertEqual(first, worktree.default_branch(one))
            self.assertNotEqual(first, worktree.default_branch(two))
            self.assertTrue(first.startswith("chore/pair-"))

    def test_the_derived_branch_is_in_the_ephemeral_class(self) -> None:
        """§20 reads the class from the NAME. A name outside the known
        patterns is an ANOMALY: a branch no rule can protect and the janitor
        may never delete. A launch worktree's branch must be cleanable."""
        import branch_classes
        with tempfile.TemporaryDirectory() as tmp:
            name = worktree.default_branch(Path(tmp))
            self.assertEqual(branch_classes.classify(name), "ephemeral")


class TestMergeGateJournalsTheTrail(PairTestCase):
    """Round 153: the merge leaves a record that never rotates.

    The transcript rotates: four archives, the oldest deleted. Ten merge
    records were lost that way before this round — a merge that fell out of
    the window is indistinguishable from a merge that escaped the gate. The
    durable journal in `.pair/worktree-log.jsonl` is the answer.
    """

    def _repo(self, tmp: Path) -> Path:
        repo = tmp / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
        subprocess.run(["git", "config", "user.email", "p@t.t"], cwd=str(repo), check=True)
        subprocess.run(["git", "config", "user.name", "P"], cwd=str(repo), check=True)
        (repo / "a.txt").write_text("x\n", encoding="utf-8")
        subprocess.run(["git", "add", "a.txt"], cwd=str(repo), check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=str(repo), check=True)
        return repo

    def test_the_merge_gate_journals_the_merge(self) -> None:
        import review_gate
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(Path(tmp))
            store = MagicMock()
            with patch.object(review_gate, "_merged_branch", return_value="room"):
                review_gate._journal_merge(store, repo, "7", "writer",
                                           pre_commits=["abc1234"])
            rows = worktree.worktree_journal_read(worktree.journal_state_dir(repo))
            merged = [r for r in rows if r["event"] == "merged"]
            committed = [r for r in rows if r["event"] == "committed"]
            self.assertEqual(len(merged), 1)
            self.assertEqual(merged[0]["pr"], "7")
            self.assertEqual(merged[0]["commits"], ["abc1234"])
            self.assertEqual([r["sha"] for r in committed], ["abc1234"])

    def test_the_commits_are_captured_before_the_branch_dies(self) -> None:
        """After the merge the branch is gone; the capture must happen
        before, or the journal names no commit at all."""
        import review_gate
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(Path(tmp))
            wt = repo / ".worktrees" / "room"
            worktree.bootstrap_worktree(repo, "room", wt)
            (wt / "b.txt").write_text("y\n", encoding="utf-8")
            subprocess.run(["git", "add", "b.txt"], cwd=str(wt), check=True)
            subprocess.run(["git", "commit", "-qm", "work"], cwd=str(wt), check=True)
            sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(wt),
                                 capture_output=True, text=True).stdout.strip()
            # Without GitHub naming the branch there is nothing to capture.
            self.assertEqual(review_gate._pre_merge_commits(repo, "7"), [])
            with patch.object(review_gate, "_merged_branch", return_value="room"):
                captured = review_gate._pre_merge_commits(repo, "7")
            self.assertEqual(captured, [sha])

    def test_a_journal_failure_is_recorded_and_never_fails_the_merge(self) -> None:
        """The merge already happened; a lost record must not hide it, but it
        must not be silent either."""
        import review_gate
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(Path(tmp))
            store = MagicMock()
            with patch.object(review_gate, "_merged_branch", return_value="room"), \
                 patch("worktree.journal_merge", side_effect=OSError("disk full")):
                review_gate._journal_merge(store, repo, "7", "writer")
            kinds = [call.args[0] for call in store.append_transcript.call_args_list]
            self.assertIn("worktree_journal_failed", kinds)


if __name__ == "__main__":
    unittest.main()
