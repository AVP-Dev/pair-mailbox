"""Tests for scripts/branch-class-check.py (PROTOCOL §20).

Behavioural, through git itself: a bare repo stands in for origin, real pushes
seed remote refs, and the checker runs over the work repo. Mocking the branch
list would test the regexes and nothing about git's actual answers — and it was
exactly git's answer (`origin` as a bare remote line) that broke the first draft.

Same shape as bin/pre_push_guard_test.py: no network, local refs only.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "branch-class-check.py"

BRANCHES = [
    "master",                     # trunk
    "release/1.2", "support/1.x",  # strategic
    "round-117-branch-classes",    # ephemeral (our own naming, dash)
    "feature/login", "fix/race", "hotfix/urgent", "chore/deps",  # ephemeral (people, slash)
    "users/andy/wip",             # personal
]


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True)


def _repo_with_branches(tmp: Path, branches: list[str]) -> Path:
    origin = tmp / "origin.git"
    _git(tmp, "init", "-q", "--bare", str(origin))
    work = tmp / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "master")
    _git(work, "config", "user.email", "t@t")
    _git(work, "config", "user.name", "t")
    (work / "f.txt").write_text("1", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "base")
    _git(work, "remote", "add", "origin", str(origin))
    for branch in branches:
        _git(work, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")
    _git(work, "fetch", "-q", "origin")
    # A real `git clone` leaves refs/remotes/origin/HEAD, which
    # `git branch -r` prints as a bare `origin` line; the shape that broke
    # draft one. set-head creates that ref the way the clone did.
    _git(work, "remote", "set-head", "origin", "master")
    return work


def _run(repo: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(repo), *extra],
        capture_output=True, text=True, timeout=120,
    )


class TestBranchClassCheck(unittest.TestCase):
    def test_the_legal_repo_reports_zero_anomaly(self) -> None:
        """Every §20 class present, and a clean exit — the shape a healthy
        repo must answer, so the counters are pinned, not just the exit code."""
        with tempfile.TemporaryDirectory() as tmp:
            work = _repo_with_branches(Path(tmp), BRANCHES)
            r = _run(work)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("0 anomaly", r.stdout)
            self.assertIn("5 ephemeral", r.stdout)  # 1 dash + 4 slash forms
            self.assertIn("2 strategic", r.stdout)
            self.assertIn("1 trunk", r.stdout)
            self.assertIn("1 personal", r.stdout)

    def test_accumulator_is_an_anomaly(self) -> None:
        """§20 rule 4: `dev` is named an antipattern, not a grey area. It must
        be ANOMALY and it must fail when asked to."""
        with tempfile.TemporaryDirectory() as tmp:
            work = _repo_with_branches(Path(tmp), ["master", "dev"])
            r = _run(work, "--fail-on-anomaly")
            self.assertEqual(r.returncode, 1, r.stdout)
            self.assertIn("ANOMALY dev", r.stdout)
            self.assertIn("antipattern", r.stdout)

    def test_unknown_name_is_an_anomaly(self) -> None:
        """A name outside every pattern protects nothing, so it is reported —
        and never silently classified as 'probably ephemeral'."""
        with tempfile.TemporaryDirectory() as tmp:
            work = _repo_with_branches(Path(tmp), ["master", "qwerty"])
            r = _run(work, "--fail-on-anomaly")
            self.assertEqual(r.returncode, 1, r.stdout)
            self.assertIn("ANOMALY qwerty", r.stdout)

    def test_without_the_flag_anomaly_is_reported_not_fatal(self) -> None:
        """The audit reports by default and refuses only when asked: a sweep
        that hard-fails on one odd name trains its operator to pass the flag."""
        with tempfile.TemporaryDirectory() as tmp:
            work = _repo_with_branches(Path(tmp), ["master", "dev"])
            r = _run(work)
            self.assertEqual(r.returncode, 0, r.stdout)
            self.assertIn("ANOMALY dev", r.stdout)

    def test_bare_remote_line_is_not_a_branch(self) -> None:
        """Regression, first-draft defect: `git branch -r` also prints a bare
        `origin` line for the remote itself. It has no class, and calling it an
        ANOMALY made the clean repo fail."""
        with tempfile.TemporaryDirectory() as tmp:
            work = _repo_with_branches(Path(tmp), ["master"])
            listing = _git(work, "branch", "-r", "--format=%(refname:short)").stdout
            self.assertIn("\norigin\n", "\n" + listing,
                          "fixture must actually produce the bare remote line")
            r = _run(work, "--fail-on-anomaly")
            self.assertEqual(r.returncode, 0, r.stdout)
            self.assertNotIn("ANOMALY origin", r.stdout)
            self.assertIn("1 trunk", r.stdout)

    def test_a_failing_git_refuses_rather_than_passes_on_silence(self) -> None:
        """The one direction that must not pass: `git branch -r` failing answers
        empty, and empty reads as 'nothing to check'. A refused check (rc=2)
        says the names were never known.

        Driven through a git that cannot run, not a mocked module: a broken
        PATH makes every `git` invocation fail for real, with no patching.
        """
        import os
        with tempfile.TemporaryDirectory() as tmp:
            _repo_with_branches(Path(tmp), ["master"])
            env = dict(os.environ)
            env["PATH"] = str(Path(tmp) / "empty")  # a dir with no git in it
            (Path(tmp) / "empty").mkdir()
            r = subprocess.run(
                [sys.executable, str(SCRIPT), "--repo", str(Path(tmp) / "work"),
                 "--fail-on-anomaly"],
                capture_output=True, text=True, timeout=120, env=env,
            )
            self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
            self.assertIn("refusing to check", r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
