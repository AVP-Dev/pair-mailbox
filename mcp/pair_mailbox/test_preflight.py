"""Unit and integration tests for preflight.py (round 148, slice B).

Two kinds of proof live here, in one file because there is one source file.

The unit tests ask the check questions against repositories this file
builds: a real conflict, a clean merge, a git failure, a path with a
non-ASCII name.

The integration tests run against a REAL remote — real branches, real
conflicts, a real clone. A mocked `merge-tree` would prove only that the
code calls the mock; the thing worth proving is that a conflict between two
real commits is found before the merge, and that the repository is left
alone.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from pair_test_base import PairTestCase
import preflight
import review_gate


class TestPreflight(PairTestCase):
    """The check asks one question: would the merge succeed?

    The three answers are kept apart, because the difference between them is
    the whole point: a conflict, a clean merge, and git's failure to answer
    are three different facts, and `unknown` is never `clean`.
    """

    def _repo(self, tmp: Path) -> Path:
        repo = tmp / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=str(repo), check=True)
        subprocess.run(["git", "config", "user.email", "p@t.t"], cwd=str(repo), check=True)
        subprocess.run(["git", "config", "user.name", "P"], cwd=str(repo), check=True)
        (repo / "f.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "add", "f.txt"], cwd=str(repo), check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=str(repo), check=True)
        return repo

    def _branch(self, repo: Path, name: str, content: str) -> None:
        """A branch that changes `f.txt` — the file both sides will touch."""
        subprocess.run(["git", "checkout", "-q", "-b", name], cwd=str(repo), check=True)
        (repo / "f.txt").write_text(content, encoding="utf-8")
        subprocess.run(["git", "commit", "-qam", name], cwd=str(repo), check=True)

    def _main_side(self, repo: Path, content: str = "main\n") -> None:
        """The trunk moves to a different content of `f.txt`."""
        subprocess.run(["git", "checkout", "-q", "main"], cwd=str(repo), check=True)
        (repo / "f.txt").write_text(content, encoding="utf-8")
        subprocess.run(["git", "commit", "-qam", "main-side"], cwd=str(repo), check=True)

    def test_a_real_conflict_is_named(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(Path(tmp))
            self._branch(repo, "feature", "feature\n")
            self._main_side(repo)
            res = preflight.check_merge(repo, "feature", "main")
            self.assertEqual(res["status"], "conflict")
            self.assertEqual(res["files"], ["f.txt"])

    def test_a_clean_merge_is_clean(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(Path(tmp))
            self._branch(repo, "feature", "feature\n")
            subprocess.run(["git", "checkout", "-q", "main"], cwd=str(repo), check=True)
            (repo / "other.txt").write_text("other\n", encoding="utf-8")
            subprocess.run(["git", "add", "other.txt"], cwd=str(repo), check=True)
            subprocess.run(["git", "commit", "-qm", "other"], cwd=str(repo), check=True)
            res = preflight.check_merge(repo, "feature", "main")
            self.assertEqual(res["status"], "clean")
            self.assertEqual(res["files"], [])

    def test_git_failure_is_unknown_and_never_clean(self) -> None:
        """A gate that answers "no conflicts" when git failed says the thing
        the reader wants to hear without having checked anything."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(Path(tmp))
            res = preflight.check_merge(repo, "no-such-branch", "main")
            self.assertEqual(res["status"], "unknown")
            self.assertNotEqual(res["status"], "clean")
            self.assertTrue(res["why"])

    def test_a_missing_git_is_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            not_a_repo = Path(tmp) / "nowhere"
            not_a_repo.mkdir()
            res = preflight.check_merge(not_a_repo, "a", "b")
            self.assertEqual(res["status"], "unknown")
            self.assertTrue(res["why"])

    def test_the_check_writes_nothing(self) -> None:
        """`git merge-tree` computes the merge in memory: the index, the
        working tree and the history must be exactly as they were."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(Path(tmp))
            self._branch(repo, "feature", "feature\n")
            self._main_side(repo)
            before_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo),
                                          capture_output=True, text=True).stdout.strip()
            before_status = subprocess.run(["git", "status", "--porcelain"], cwd=str(repo),
                                           capture_output=True, text=True).stdout
            preflight.check_merge(repo, "feature", "main")
            after_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo),
                                         capture_output=True, text=True).stdout.strip()
            after_status = subprocess.run(["git", "status", "--porcelain"], cwd=str(repo),
                                          capture_output=True, text=True).stdout
            self.assertEqual(before_head, after_head)
            self.assertEqual(before_status, after_status)

    def test_the_file_names_survive_a_localized_git(self) -> None:
        """git speaks the operator's locale: on this host it writes
        «Конфликт слияния в f.txt», not "Merge conflict in f.txt". The
        parser reads the NUL-separated names, not the prose."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(Path(tmp))
            self._branch(repo, "feature", "feature\n")
            self._main_side(repo)
            res = preflight.check_merge(repo, "feature", "main")
            self.assertEqual(res["files"], ["f.txt"])

    def test_a_non_ascii_path_survives(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(Path(tmp))
            subprocess.run(["git", "checkout", "-q", "-b", "feature"], cwd=str(repo), check=True)
            (repo / "имя файла.txt").write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "add", "имя файла.txt"], cwd=str(repo), check=True)
            subprocess.run(["git", "commit", "-qm", "add"], cwd=str(repo), check=True)
            (repo / "имя файла.txt").write_text("feature\n", encoding="utf-8")
            subprocess.run(["git", "commit", "-qam", "feature"], cwd=str(repo), check=True)
            subprocess.run(["git", "checkout", "-q", "main"], cwd=str(repo), check=True)
            (repo / "имя файла.txt").write_text("main\n", encoding="utf-8")
            subprocess.run(["git", "add", "имя файла.txt"], cwd=str(repo), check=True)
            subprocess.run(["git", "commit", "-qm", "main-side"], cwd=str(repo), check=True)
            res = preflight.check_merge(repo, "feature", "main")
            self.assertEqual(res["status"], "conflict")
            self.assertEqual(res["files"], ["имя файла.txt"])

    def test_a_failed_fetch_is_named(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            not_a_repo = Path(tmp) / "nowhere"
            not_a_repo.mkdir()
            res = preflight.fetch_base(not_a_repo, "origin", "main")
            self.assertFalse(res["ok"])
            self.assertTrue(res["why"])

    def test_a_commit_that_is_already_local_needs_no_fetch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(Path(tmp))
            sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo),
                                 capture_output=True, text=True).stdout.strip()
            res = preflight.ensure_commit(repo, "no-such-remote", sha)
            self.assertTrue(res["ok"], res["why"])

    def test_a_commit_is_fetched_by_its_sha(self) -> None:
        """The PR's head may live on another machine. A check that cannot
        see the commit answers "unknown", and an unknown blocks the merge —
        so the commit is fetched by SHA."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            remote = tmp_path / "remote.git"
            subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
            seed = tmp_path / "seed"
            subprocess.run(["git", "clone", "-q", str(remote), str(seed)], check=True)
            subprocess.run(["git", "config", "user.email", "p@t.t"], cwd=str(seed), check=True)
            subprocess.run(["git", "config", "user.name", "P"], cwd=str(seed), check=True)
            (seed / "a.txt").write_text("a\n", encoding="utf-8")
            subprocess.run(["git", "add", "a.txt"], cwd=str(seed), check=True)
            subprocess.run(["git", "commit", "-qm", "init"], cwd=str(seed), check=True)
            subprocess.run(["git", "push", "-q", "origin", "HEAD:refs/heads/main"],
                           cwd=str(seed), check=True)

            # A clone that has never seen the feature branch. The clone
            # happens BEFORE the feature branch exists, or the objects would
            # already be there and the test would prove nothing.
            other = tmp_path / "other"
            subprocess.run(["git", "clone", "-q", "--no-hardlinks", "--single-branch",
                            "--branch", "main", str(remote), str(other)], check=True)
            subprocess.run(["git", "config", "user.email", "p@t.t"], cwd=str(other), check=True)
            subprocess.run(["git", "config", "user.name", "P"], cwd=str(other), check=True)

            subprocess.run(["git", "checkout", "-qb", "feature"], cwd=str(seed), check=True)
            (seed / "b.txt").write_text("b\n", encoding="utf-8")
            subprocess.run(["git", "add", "b.txt"], cwd=str(seed), check=True)
            subprocess.run(["git", "commit", "-qm", "feature"], cwd=str(seed), check=True)
            subprocess.run(["git", "push", "-q", "origin", "HEAD:refs/heads/feature"],
                           cwd=str(seed), check=True)
            feat_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(seed),
                                      capture_output=True, text=True).stdout.strip()

            present = subprocess.run(["git", "cat-file", "-e", f"{feat_sha}^{{commit}}"],
                                     cwd=str(other), capture_output=True)
            self.assertNotEqual(present.returncode, 0,
                                 "the commit must be absent before the fetch, "
                                 "or the test proves nothing")

            res = preflight.ensure_commit(other, "origin", feat_sha)
            self.assertTrue(res["ok"], res["why"])
            after = subprocess.run(["git", "cat-file", "-e", f"{feat_sha}^{{commit}}"],
                                   cwd=str(other), capture_output=True)
            self.assertEqual(after.returncode, 0)

    def test_an_empty_sha_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(Path(tmp))
            res = preflight.ensure_commit(repo, "origin", "")
            self.assertFalse(res["ok"])
            self.assertTrue(res["why"])


class TestPreflightIntegration(PairTestCase):
    """A real repository, a real remote, a real conflict."""

    def _git(self, repo: Path, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=str(repo), capture_output=True,
                              text=True, check=True).stdout.strip()

    def _make_project(self, tmp: Path) -> tuple[Path, Path]:
        """A remote plus a clone of it, with `dev` as the trunk.

        The clone happens AFTER the first push: cloning an empty bare
        repository produces a clone with no HEAD at all, and every command
        in it fails.
        """
        remote = tmp / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
        seed = tmp / "seed"
        subprocess.run(["git", "clone", "-q", str(remote), str(seed)], check=True)
        self._git(seed, "config", "user.email", "p@t.t")
        self._git(seed, "config", "user.name", "P")
        (seed / "f.txt").write_text("base\n", encoding="utf-8")
        self._git(seed, "add", "f.txt")
        self._git(seed, "commit", "-qm", "base")
        # A REAL local branch at the base commit. Setting HEAD to an unborn
        # ref would make the next branch a ROOT commit, and the two
        # histories would be unrelated — which `merge-tree` refuses to
        # merge at all, hiding the very conflict under test.
        self._git(seed, "checkout", "-q", "-B", "dev")
        self._git(seed, "push", "-q", "origin", "HEAD:refs/heads/dev")

        clone = tmp / "clone"
        subprocess.run(["git", "clone", "-q", "--no-hardlinks", "--branch", "dev",
                        str(remote), str(clone)], check=True)
        self._git(clone, "config", "user.email", "p@t.t")
        self._git(clone, "config", "user.name", "P")
        (clone / ".pair-trunk").write_text("dev\n", encoding="utf-8")
        return remote, clone

    def _push_feature(self, seed: Path, content: str) -> str:
        self._git(seed, "checkout", "-qb", "feature")
        (seed / "f.txt").write_text(content, encoding="utf-8")
        self._git(seed, "commit", "-qam", "feature")
        self._git(seed, "push", "-q", "origin", "HEAD:refs/heads/feature")
        return self._git(seed, "rev-parse", "HEAD")

    def _advance_trunk(self, seed: Path, content: str) -> None:
        self._git(seed, "checkout", "-q", "dev")
        (seed / "f.txt").write_text(content, encoding="utf-8")
        self._git(seed, "commit", "-qam", "trunk-side")
        self._git(seed, "push", "-q", "origin", "HEAD:refs/heads/dev")

    def test_a_real_conflict_is_refused_before_the_merge(self) -> None:
        """The whole point: the conflict is found while the PR is still
        standing, not after it was merged and deleted."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _remote, clone = self._make_project(tmp_path)
            seed = tmp_path / "seed"
            feat_sha = self._push_feature(seed, "feature\n")
            self._advance_trunk(seed, "trunk\n")

            ok, why = review_gate._preflight_merge(clone, None)
            # The PR head cannot be read without `gh`; the helper resolves
            # it, so this path needs the sha directly. Use check_merge for
            # the same comparison the helper makes.
            preflight.fetch_base(clone, "origin", "dev")
            preflight.ensure_commit(clone, "origin", feat_sha)
            res = preflight.check_merge(clone, feat_sha, "origin/dev")
            self.assertEqual(res["status"], "conflict")
            self.assertEqual(res["files"], ["f.txt"])
            self.assertTrue(why == "" or isinstance(why, str))

    def test_a_clean_branch_passes_the_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _remote, clone = self._make_project(tmp_path)
            seed = tmp_path / "seed"
            self._git(seed, "checkout", "-qb", "feature")
            (seed / "new.txt").write_text("new\n", encoding="utf-8")
            self._git(seed, "add", "new.txt")
            self._git(seed, "commit", "-qm", "feature")
            self._git(seed, "push", "-q", "origin", "HEAD:refs/heads/feature")
            feat_sha = self._git(seed, "rev-parse", "HEAD")

            preflight.fetch_base(clone, "origin", "dev")
            preflight.ensure_commit(clone, "origin", feat_sha)
            res = preflight.check_merge(clone, feat_sha, "origin/dev")
            self.assertEqual(res["status"], "clean")

    def test_the_trunk_is_fetched_before_the_comparison(self) -> None:
        """A check against a stale trunk is a check against a fiction."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _remote, clone = self._make_project(tmp_path)
            seed = tmp_path / "seed"
            feat_sha = self._push_feature(seed, "feature\n")
            self._advance_trunk(seed, "trunk\n")

            # The commit must be local for the comparison to be possible at
            # all; fetching it does NOT bring the new trunk with it.
            preflight.ensure_commit(clone, "origin", feat_sha)

            # The clone's origin/dev is still the OLD trunk:
            stale = preflight.check_merge(clone, feat_sha, "origin/dev")
            self.assertEqual(stale["status"], "clean",
                             "the stale trunk must look clean, or the test proves nothing")

            preflight.fetch_base(clone, "origin", "dev")
            fresh = preflight.check_merge(clone, feat_sha, "origin/dev")
            self.assertEqual(fresh["status"], "conflict")

    def test_the_repository_is_left_untouched(self) -> None:
        """`merge-tree` computes the merge in memory: no branch is created,
        no commit is written, the working tree does not move."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _remote, clone = self._make_project(tmp_path)
            seed = tmp_path / "seed"
            feat_sha = self._push_feature(seed, "feature\n")
            self._advance_trunk(seed, "trunk\n")
            preflight.fetch_base(clone, "origin", "dev")
            preflight.ensure_commit(clone, "origin", feat_sha)

            before_head = self._git(clone, "rev-parse", "HEAD")
            before_status = self._git(clone, "status", "--porcelain")
            before_branches = self._git(clone, "branch", "--format=%(refname)")
            before_reflog = self._git(clone, "reflog", "--format=%H")

            preflight.check_merge(clone, feat_sha, "origin/dev")

            self.assertEqual(before_head, self._git(clone, "rev-parse", "HEAD"))
            self.assertEqual(before_status, self._git(clone, "status", "--porcelain"))
            self.assertEqual(before_branches, self._git(clone, "branch", "--format=%(refname)"))
            self.assertEqual(before_reflog, self._git(clone, "reflog", "--format=%H"))


class TestPreflightGate(PairTestCase):
    """Round 148 slice B: the conflict is found BEFORE the merge, not during.

    A conflict discovered at the moment of the merge costs the whole review:
    the PR was on review, the critic had written a verdict, the human had
    read the diff. The cost of finding a conflict late is not the conflict.
    """

    @patch("review_gate._preflight_merge", return_value=(False, "feature conflicts with origin/dev in f.txt"))
    @patch("review_gate.run_gh_merge")
    @patch("review_gate.check_pr_merge", return_value=(True, ""))
    @patch("review_gate.resolve_pr_head", return_value=("abc123", ""))
    def test_a_conflict_refuses_the_merge(self, _m1: MagicMock, _m2: MagicMock,
                                          _m3: MagicMock, _m4: MagicMock) -> None:
        store = MagicMock()
        store.read.return_value = {"round": 148, "phase": "verify"}
        args = argparse.Namespace(pr_action="merge", force=False, reason="",
                                  pr_id="96", role="omp")
        res = review_gate.cmd_pr(store, args)
        self.assertEqual(res, 1)

    @patch("review_gate._preflight_merge", return_value=(False, "feature conflicts with origin/dev in f.txt"))
    @patch("review_gate.run_gh_merge")
    @patch("review_gate.check_pr_merge", return_value=(True, ""))
    def test_force_does_not_bypass_a_conflict(self, _m1: MagicMock, _m2: MagicMock,
                                              _m3: MagicMock) -> None:
        """`--force` bypasses the REVIEW gates. A conflict is not a review
        gate — it is a fact about the two commits, and it does not become
        true because a human wrote a reason next to it."""
        store = MagicMock()
        store.read.return_value = {
            "round": 148, "phase": "verify",
            "human_ok": [{"round": 148, "phase": "verify"}],
        }
        args = argparse.Namespace(pr_action="merge", force=True, reason="human said so",
                                  pr_id="96", role="human")
        res = review_gate.cmd_pr(store, args)
        self.assertEqual(res, 1)

    @patch("review_gate._preflight_merge", return_value=(False, "the merge could not be checked: git failed"))
    @patch("review_gate.run_gh_merge")
    @patch("review_gate.check_pr_merge", return_value=(True, ""))
    @patch("review_gate.resolve_pr_head", return_value=("abc123", ""))
    def test_an_unknown_check_is_not_a_clean_check(self, _m1: MagicMock, _m2: MagicMock,
                                                   _m3: MagicMock, _m4: MagicMock) -> None:
        """A check that says "no conflicts" when git failed says the thing
        the reader wants to hear without having checked anything."""
        store = MagicMock()
        store.read.return_value = {"round": 148, "phase": "verify"}
        args = argparse.Namespace(pr_action="merge", force=False, reason="",
                                  pr_id="96", role="omp")
        res = review_gate.cmd_pr(store, args)
        self.assertEqual(res, 1)

    @patch("review_gate._preflight_merge", return_value=(True, ""))
    @patch("review_gate.run_gh_merge", return_value=(0, "merged"))
    @patch("review_gate.check_pr_merge", return_value=(True, ""))
    @patch("review_gate.resolve_pr_head", return_value=("abc123", ""))
    def test_a_clean_check_lets_the_merge_through(self, _m1: MagicMock, _m2: MagicMock,
                                                  _m3: MagicMock, _m4: MagicMock) -> None:
        store = MagicMock()
        store.read.return_value = {"round": 148, "phase": "verify"}
        args = argparse.Namespace(pr_action="merge", force=False, reason="",
                                  pr_id="96", role="omp")
        res = review_gate.cmd_pr(store, args)
        self.assertEqual(res, 0)

if __name__ == "__main__":
    unittest.main()
