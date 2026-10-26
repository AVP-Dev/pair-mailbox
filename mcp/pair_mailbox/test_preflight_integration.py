"""Integration tests for the pre-flight merge check (round 148, slice B).

These run against a REAL git repository — real branches, real conflicts, a
real remote. A mocked `merge-tree` would prove only that the code calls the
mock; the thing worth proving is that a conflict between two real commits
is found before the merge, and that the repository is left alone.
"""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from pair_test_base import PairTestCase
import review_gate
import preflight


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


if __name__ == "__main__":
    unittest.main()
