"""Tests for trunk.py — the project's trunk, resolved once, from four places.

The order is the contract: the environment wins for one process, the local
ignored file wins for one workspace, the committed file wins for everybody,
and `master` is what a project that says nothing gets.

Every case builds a real directory tree; nothing about the resolver is
mocked, because the whole contract is "which file is read, and in which
order".

Run: python3 mcp/pair_mailbox/test_trunk.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import trunk  # noqa: E402


class TestResolveTrunk(unittest.TestCase):
    """The four sources and their order."""

    def setUp(self) -> None:
        self._env = os.environ.get(trunk.ENV_VAR)
        os.environ.pop(trunk.ENV_VAR, None)
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self._restore_env)

    def _restore_env(self) -> None:
        if self._env is None:
            os.environ.pop(trunk.ENV_VAR, None)
        else:
            os.environ[trunk.ENV_VAR] = self._env

    def _write(self, rel: str, text: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_default_is_master_when_nothing_is_set(self) -> None:
        self.assertEqual(trunk.resolve_trunk(self.root), "master")

    def test_environment_wins_over_both_files(self) -> None:
        self._write(".pair/trunk", "local\n")
        self._write(".pair-trunk", "project\n")
        os.environ[trunk.ENV_VAR] = "from-env"
        self.assertEqual(trunk.resolve_trunk(self.root), "from-env")

    def test_local_file_wins_over_the_project_file(self) -> None:
        self._write(".pair/trunk", "local\n")
        self._write(".pair-trunk", "project\n")
        self.assertEqual(trunk.resolve_trunk(self.root), "local")

    def test_project_file_is_used_when_no_local_file(self) -> None:
        self._write(".pair-trunk", "dev\n")
        self.assertEqual(trunk.resolve_trunk(self.root), "dev")

    def test_empty_files_fall_through_to_the_next_source(self) -> None:
        self._write(".pair/trunk", "\n  \n")
        self._write(".pair-trunk", "dev\n")
        self.assertEqual(trunk.resolve_trunk(self.root), "dev")

    def test_only_the_first_line_is_the_name(self) -> None:
        self._write(".pair-trunk", "dev\n# a comment\n")
        self.assertEqual(trunk.resolve_trunk(self.root), "dev")

    def test_a_missing_file_is_not_a_name(self) -> None:
        self.assertEqual(trunk.resolve_trunk(self.root / "nowhere"), "master")

    def test_trunk_ref_names_the_remote_branch(self) -> None:
        self._write(".pair-trunk", "dev\n")
        self.assertEqual(trunk.trunk_ref(self.root), "origin/dev")
        self.assertEqual(trunk.trunk_ref(self.root, remote="upstream"),
                         "upstream/dev")

    def test_trunk_ref_follows_the_default(self) -> None:
        self.assertEqual(trunk.trunk_ref(self.root), "origin/master")


class TestTrunkIsUsedByTheGates(unittest.TestCase):
    """The gates that used to say `origin/master` must follow the trunk.

    Red-proof for round 144: a project whose trunk is `dev` could merge a
    round into `dev` and never close it, because the close-side gates
    compared against `origin/master` by name.
    """

    def setUp(self) -> None:
        self._env = os.environ.get(trunk.ENV_VAR)
        os.environ.pop(trunk.ENV_VAR, None)
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self._restore_env)

    def _restore_env(self) -> None:
        if self._env is None:
            os.environ.pop(trunk.ENV_VAR, None)
        else:
            os.environ[trunk.ENV_VAR] = self._env

    def test_origin_truth_compares_against_the_trunk(self) -> None:
        import review

        subprocess_git = review._run_git
        seen: list[str] = []

        def _spy(root: Path, *args: str) -> str | None:
            seen.append(" ".join(args))
            return subprocess_git(root, *args)

        review._run_git = _spy
        self.addCleanup(setattr, review, "_run_git", subprocess_git)

        self._git(self.root, "init", "-q", "-b", "dev")
        self._git(self.root, "config", "user.email", "t@t.t")
        self._git(self.root, "config", "user.name", "t")
        (self.root / "a.py").write_text("x=1\n", encoding="utf-8")
        self._git(self.root, "add", "-A")
        self._git(self.root, "commit", "-qm", "base")
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(self.root),
                              capture_output=True, text=True).stdout.strip()
        self._git(self.root, "update-ref", "refs/remotes/origin/dev", head)

        (self.root / ".pair-trunk").write_text("dev\n", encoding="utf-8")
        status, detail = review.origin_truth(self.root, head)
        self.assertEqual(status, "aligned", detail)
        self.assertTrue(any("origin/dev" in call for call in seen),
                        f"the origin-truth probe never asked for origin/dev: {seen}")
        self.assertFalse(any("origin/master" in call for call in seen),
                         f"the origin-truth probe still asked for origin/master: {seen}")

    def _git(self, cwd: Path, *args: str) -> None:
        proc = subprocess.run(["git", *args], cwd=str(cwd),
                              capture_output=True, text=True)
        if proc.returncode != 0:
            self.fail(f"git {' '.join(args)} failed: {proc.stderr}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
