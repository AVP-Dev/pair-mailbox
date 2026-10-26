"""Unit tests for preflight.py (the merge-conflict pre-flight — round 148)."""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from pair_test_base import PairTestCase
import preflight


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


if __name__ == "__main__":
    unittest.main()
