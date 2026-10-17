#!/usr/bin/env python3
"""Tests for bin/git-hooks/pre-push (round 105 slice 2a).

Behavioural, through git itself: a bare repo stands in for origin, the hook is
installed into it as .git/hooks/pre-push, and real pushes are attempted. The
forbidden direction must be refused before bytes travel; the accumulator branch
must pass untouched. No mock of git — the whole point is the guard fires on the
real transport contract.
Run: python3 bin/pre_push_guard_test.py
"""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / "bin" / "git-hooks" / "pre-push"


def _git(cwd: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    import os
    e = dict(os.environ)
    e.setdefault("GIT_AUTHOR_NAME", "t")
    e.setdefault("GIT_AUTHOR_EMAIL", "t@t")
    e.setdefault("GIT_COMMITTER_NAME", "t")
    e.setdefault("GIT_COMMITTER_EMAIL", "t@t")
    if env:
        e.update(env)
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, env=e)


class TestPrePushLaneGuard(unittest.TestCase):
    def _repo_with_hook(self, tmp: Path) -> tuple[Path, Path]:
        """A work repo on both master and dev, hook installed, bare origin."""
        origin = tmp / "origin.git"
        _git(tmp, "init", "-q", "--bare", str(origin))
        work = tmp / "work"
        work.mkdir()
        _git(work, "init", "-q", "-b", "master")
        (work / "f.txt").write_text("1", encoding="utf-8")
        _git(work, "add", "-A")
        _git(work, "commit", "-q", "-m", "base")
        _git(work, "checkout", "-q", "-b", "dev")
        (work / "g.txt").write_text("2", encoding="utf-8")
        _git(work, "add", "-A")
        _git(work, "commit", "-q", "-m", "dev work")
        hooks = work / ".git" / "hooks"
        hooks.mkdir(parents=True, exist_ok=True)
        dest = hooks / "pre-push"
        dest.write_bytes(HOOK.read_bytes())
        dest.chmod(0o755)
        _git(work, "remote", "add", "origin", str(origin))
        return work, origin

    def test_direct_master_push_refused_before_travel(self) -> None:
        """The forbidden direction is refused AND origin never receives master:
        the guard fires before bytes leave, not after."""
        with tempfile.TemporaryDirectory() as tmp:
            work, origin = self._repo_with_hook(Path(tmp))
            r = _git(work, "push", "origin", "master")
            self.assertNotEqual(r.returncode, 0, "master push must fail")
            self.assertIn("REFUSED", r.stderr)
            self.assertIn("ONLY by PR merge", r.stderr)
            listed = _git(work, "ls-remote", "--heads", "origin", "master")
            self.assertEqual(listed.stdout.strip(), "", "master reached origin")

    def test_accumulator_branch_push_passes(self) -> None:
        """dev is the lane: same hook, same repo, push succeeds in silence."""
        with tempfile.TemporaryDirectory() as tmp:
            work, _ = self._repo_with_hook(Path(tmp))
            r = _git(work, "push", "-q", "origin", "dev")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertNotIn("REFUSED", r.stderr)
            listed = _git(work, "ls-remote", "--heads", "origin", "dev")
            self.assertIn("refs/heads/dev", listed.stdout)

    def test_refspec_form_also_refused(self) -> None:
        """HEAD:refs/heads/master updates master and must be caught too —
        the guard keys on the REMOTE ref, not the local one."""
        with tempfile.TemporaryDirectory() as tmp:
            work, _ = self._repo_with_hook(Path(tmp))
            r = _git(work, "push", "origin", "HEAD:refs/heads/master")
            self.assertNotEqual(r.returncode, 0, "explicit master refspec must fail")
            self.assertIn("REFUSED", r.stderr)

    def test_human_override_is_echoed_not_silent(self) -> None:
        """The override works, and announces itself on stderr every time."""
        with tempfile.TemporaryDirectory() as tmp:
            work, _ = self._repo_with_hook(Path(tmp))
            r = _git(work, "push", "origin", "master",
                     env={"PAIR_ALLOW_MASTER_PUSH": "1"})
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("PAIR_ALLOW_MASTER_PUSH=1", r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
