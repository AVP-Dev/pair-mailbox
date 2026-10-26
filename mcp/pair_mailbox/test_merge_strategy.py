"""Unit tests for merge_strategy.py (round 148, slice C)."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from pair_test_base import PairTestCase
import merge_strategy
import project_settings
import review_gate


class TestMergeStrategy(PairTestCase):
    """The strategy comes from the project; an impossible one is refused.

    The two things worth proving are the default (nothing changes for a
    project that says nothing) and the refusal (a project that asks for a
    strategy that does not exist is told, rather than quietly given
    another one).
    """

    def test_the_default_is_squash(self) -> None:
        """The behaviour that existed before the setting existed, byte for
        byte."""
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop(project_settings.env_var(merge_strategy.SETTING), None)
                self.assertEqual(merge_strategy.resolve_merge_strategy(Path(tmp)), "squash")
                self.assertEqual(merge_strategy.merge_flags(Path(tmp)),
                                 ["--squash", "--delete-branch"])

    def test_the_environment_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".merge_strategy").write_text("merge\n", encoding="utf-8")
            with patch.dict(os.environ, {project_settings.env_var(merge_strategy.SETTING): "rebase"}):
                self.assertEqual(merge_strategy.resolve_merge_strategy(root), "rebase")
                self.assertEqual(merge_strategy.merge_flags(root),
                                 ["--rebase", "--delete-branch"])

    def test_the_local_file_wins_over_the_committed_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".pair").mkdir()
            (root / ".pair" / "merge_strategy").write_text("merge\n", encoding="utf-8")
            (root / ".merge_strategy").write_text("rebase\n", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop(project_settings.env_var(merge_strategy.SETTING), None)
                self.assertEqual(merge_strategy.resolve_merge_strategy(root), "merge")

    def test_the_committed_file_wins_over_the_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".merge_strategy").write_text("rebase\n", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop(project_settings.env_var(merge_strategy.SETTING), None)
                self.assertEqual(merge_strategy.resolve_merge_strategy(root), "rebase")

    def test_the_first_non_empty_line_is_the_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".merge_strategy").write_text("\n\n  merge  \n", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop(project_settings.env_var(merge_strategy.SETTING), None)
                self.assertEqual(merge_strategy.resolve_merge_strategy(root), "merge")

    def test_an_invalid_strategy_is_refused_with_the_valid_ones_named(self) -> None:
        """A silent fall back to squash would mean a project that asked for
        `rebase` gets `squash` and learns it from the trunk's history."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".merge_strategy").write_text("cherry-pick\n", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop(project_settings.env_var(merge_strategy.SETTING), None)
                with self.assertRaises(ValueError) as ctx:
                    merge_strategy.resolve_merge_strategy(root)
            message = str(ctx.exception)
            self.assertIn("cherry-pick", message)
            for name in ("squash", "merge", "rebase"):
                self.assertIn(name, message)

    def test_an_invalid_strategy_in_the_environment_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {project_settings.env_var(merge_strategy.SETTING): "nonsense"}):
                with self.assertRaises(ValueError):
                    merge_strategy.resolve_merge_strategy(Path(tmp))

    def test_delete_branch_is_always_there(self) -> None:
        """§19: the round's branch dies with the round, whatever strategy
        brought it to the trunk."""
        for name in ("squash", "merge", "rebase"):
            with tempfile.TemporaryDirectory() as tmp:
                with patch.dict(os.environ, {project_settings.env_var(merge_strategy.SETTING): name}):
                    flags = merge_strategy.merge_flags(Path(tmp))
                self.assertIn("--delete-branch", flags)
                self.assertEqual(len([f for f in flags if f == "--delete-branch"]), 1)

    def test_each_strategy_has_exactly_one_strategy_flag(self) -> None:
        for name in ("squash", "merge", "rebase"):
            with tempfile.TemporaryDirectory() as tmp:
                with patch.dict(os.environ, {project_settings.env_var(merge_strategy.SETTING): name}):
                    flags = merge_strategy.merge_flags(Path(tmp))
                strategy_flags = [f for f in flags if f in ("--squash", "--merge", "--rebase")]
                self.assertEqual(strategy_flags, [f"--{name}"])

    def test_a_missing_file_is_not_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop(project_settings.env_var(merge_strategy.SETTING), None)
                self.assertEqual(merge_strategy.resolve_merge_strategy(Path(tmp)), "squash")


class TestRunGhMergeFlags(PairTestCase):
    """Round 148 slice C: the strategy reaches `gh` as the right flag.

    The setting is only real if it changes the command. A resolver that
    reads `rebase` correctly while the merge still passes `--squash` would
    satisfy every test of the resolver and ship the old behaviour.
    """

    def _capture(self, strategy: str | None) -> list[str]:
        """Run `run_gh_merge` and return the command it built."""
        seen: list[str] = []

        def fake_run(cmd, **kwargs):  # noqa: ANN001 - a mock stand-in
            seen.extend(cmd)
            return MagicMock(returncode=0, stdout="merged", stderr="")

        env = {}
        if strategy is not None:
            env["PAIR_MERGE_STRATEGY"] = strategy
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, env, clear=False):
                if strategy is None:
                    os.environ.pop("PAIR_MERGE_STRATEGY", None)
                with patch("review_gate.subprocess.run", fake_run):
                    review_gate.run_gh_merge(Path(tmp), "42")
        return seen

    def test_the_default_strategy_squashes(self) -> None:
        cmd = self._capture(None)
        self.assertIn("--squash", cmd)
        self.assertIn("--delete-branch", cmd)
        self.assertNotIn("--merge", cmd)
        self.assertNotIn("--rebase", cmd)

    def test_the_merge_strategy_merges(self) -> None:
        cmd = self._capture("merge")
        self.assertIn("--merge", cmd)
        self.assertIn("--delete-branch", cmd)
        self.assertNotIn("--squash", cmd)
        self.assertNotIn("--rebase", cmd)

    def test_the_rebase_strategy_rebases(self) -> None:
        cmd = self._capture("rebase")
        self.assertIn("--rebase", cmd)
        self.assertIn("--delete-branch", cmd)
        self.assertNotIn("--squash", cmd)
        self.assertNotIn("--merge", cmd)

    def test_an_invalid_strategy_refuses_the_merge(self) -> None:
        """The refusal names the admissible values — a refusal that does not
        say what would have worked sends the reader looking through the
        source."""
        seen: list[str] = []

        def fake_run(cmd, **kwargs):  # noqa: ANN001 - a mock stand-in
            seen.extend(cmd)
            return MagicMock(returncode=0, stdout="merged", stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"PAIR_MERGE_STRATEGY": "cherry-pick"}):
                with patch("review_gate.subprocess.run", fake_run):
                    code, out = review_gate.run_gh_merge(Path(tmp), "42")
        self.assertEqual(code, 1)
        self.assertEqual(seen, [], "gh must not run with a refused strategy")
        self.assertIn("squash", out)
        self.assertIn("merge", out)
        self.assertIn("rebase", out)

if __name__ == "__main__":
    unittest.main()
