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
        """A work repo on master, hook installed, bare origin.

        §19: master is the only long-lived branch — there is no dev to
        accumulate into, so the fixture does not build one.
        """
        origin = tmp / "origin.git"
        _git(tmp, "init", "-q", "--bare", str(origin))
        work = tmp / "work"
        work.mkdir()
        _git(work, "init", "-q", "-b", "master")
        (work / "f.txt").write_text("1", encoding="utf-8")
        _git(work, "add", "-A")
        _git(work, "commit", "-q", "-m", "base")
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
            work, origin = self._repo_with_hook(Path(tmp))
            r = _git(work, "push", "origin", "master",
                     env={"PAIR_ALLOW_MASTER_PUSH": "1"})
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("PAIR_ALLOW_MASTER_PUSH=1", r.stderr)

    def test_equal_head_push_is_not_a_guard_signal(self) -> None:
        """Seen live: master already equal to origin answers rc=0 with no hook
        run at all — a no-op push. So 'rc==0, no REFUSED' is ambiguous between
        a firing guard and an idle one. The only meaningful proof is rc!=0 on
        real divergence, and this test pins that the divergence case is what
        the suite exercises (master advanced past origin by one commit)."""
        with tempfile.TemporaryDirectory() as tmp:
            work, origin = self._repo_with_hook(Path(tmp))
            # Seed a non-master branch only: pushing master to seed origin is
            # itself the banned direction, so the fixture must not train itself on it.
            seeded = _git(work, "push", "-q", "origin", "HEAD:refs/heads/round-seed")
            self.assertEqual(seeded.returncode, 0, seeded.stderr)
            # origin/master exists via refspec-free ls-remote? No: construct it
            # directly so the local repo sees origin/master WITHOUT a banned
            # push — the merge that would normally be the only legal way in.
            _git(work, "fetch", "-q", "origin", "round-seed")
            _git(work, "update-ref", "refs/remotes/origin/master", "HEAD")
            origin_master = _git(work, "rev-parse", "refs/remotes/origin/master").stdout.strip()
            # now diverge: a commit on master that origin does not have
            _git(work, "checkout", "-q", "master")
            (work / "h.txt").write_text("hotfix", encoding="utf-8")
            _git(work, "add", "-A")
            _git(work, "commit", "-q", "-m", "post-merge hotfix")
            after_local = _git(work, "rev-parse", "master").stdout.strip()
            self.assertNotEqual(after_local, origin_master,
                                "test fixture must diverge from origin, else the push is a no-op")
            r = _git(work, "push", "origin", "master")
            self.assertNotEqual(r.returncode, 0,
                               "a diverging master push must be refused; equal-head would say nothing")
            self.assertIn("REFUSED", r.stderr)
            # The authoritative check: a push that travelled would have updated
            # the real bare origin under refs/heads/master. It has no master ref
            # at all, so absence proves no bytes landed.
            self.assertEqual(_git(work, "ls-remote", "--heads", "origin", "master").stdout.strip(),
                             "", "no refs/heads/master may exist on origin after a refused push")


if __name__ == "__main__":
    unittest.main(verbosity=2)
