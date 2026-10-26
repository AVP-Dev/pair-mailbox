"""Unit tests for merge_strategy.py (round 148, slice C)."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pair_test_base import PairTestCase
import merge_strategy


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
                os.environ.pop(merge_strategy.ENV_VAR, None)
                self.assertEqual(merge_strategy.resolve_merge_strategy(Path(tmp)), "squash")
                self.assertEqual(merge_strategy.merge_flags(Path(tmp)),
                                 ["--squash", "--delete-branch"])

    def test_the_environment_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".pair-merge-strategy").write_text("merge\n", encoding="utf-8")
            with patch.dict(os.environ, {merge_strategy.ENV_VAR: "rebase"}):
                self.assertEqual(merge_strategy.resolve_merge_strategy(root), "rebase")
                self.assertEqual(merge_strategy.merge_flags(root),
                                 ["--rebase", "--delete-branch"])

    def test_the_local_file_wins_over_the_committed_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".pair").mkdir()
            (root / ".pair" / "merge-strategy").write_text("merge\n", encoding="utf-8")
            (root / ".pair-merge-strategy").write_text("rebase\n", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop(merge_strategy.ENV_VAR, None)
                self.assertEqual(merge_strategy.resolve_merge_strategy(root), "merge")

    def test_the_committed_file_wins_over_the_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".pair-merge-strategy").write_text("rebase\n", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop(merge_strategy.ENV_VAR, None)
                self.assertEqual(merge_strategy.resolve_merge_strategy(root), "rebase")

    def test_the_first_non_empty_line_is_the_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".pair-merge-strategy").write_text("\n\n  merge  \n", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop(merge_strategy.ENV_VAR, None)
                self.assertEqual(merge_strategy.resolve_merge_strategy(root), "merge")

    def test_an_invalid_strategy_is_refused_with_the_valid_ones_named(self) -> None:
        """A silent fall back to squash would mean a project that asked for
        `rebase` gets `squash` and learns it from the trunk's history."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".pair-merge-strategy").write_text("cherry-pick\n", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop(merge_strategy.ENV_VAR, None)
                with self.assertRaises(ValueError) as ctx:
                    merge_strategy.resolve_merge_strategy(root)
            message = str(ctx.exception)
            self.assertIn("cherry-pick", message)
            for name in ("squash", "merge", "rebase"):
                self.assertIn(name, message)

    def test_an_invalid_strategy_in_the_environment_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {merge_strategy.ENV_VAR: "nonsense"}):
                with self.assertRaises(ValueError):
                    merge_strategy.resolve_merge_strategy(Path(tmp))

    def test_delete_branch_is_always_there(self) -> None:
        """§19: the round's branch dies with the round, whatever strategy
        brought it to the trunk."""
        for name in ("squash", "merge", "rebase"):
            with tempfile.TemporaryDirectory() as tmp:
                with patch.dict(os.environ, {merge_strategy.ENV_VAR: name}):
                    flags = merge_strategy.merge_flags(Path(tmp))
                self.assertIn("--delete-branch", flags)
                self.assertEqual(len([f for f in flags if f == "--delete-branch"]), 1)

    def test_each_strategy_has_exactly_one_strategy_flag(self) -> None:
        for name in ("squash", "merge", "rebase"):
            with tempfile.TemporaryDirectory() as tmp:
                with patch.dict(os.environ, {merge_strategy.ENV_VAR: name}):
                    flags = merge_strategy.merge_flags(Path(tmp))
                strategy_flags = [f for f in flags if f in ("--squash", "--merge", "--rebase")]
                self.assertEqual(strategy_flags, [f"--{name}"])

    def test_a_missing_file_is_not_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop(merge_strategy.ENV_VAR, None)
                self.assertEqual(merge_strategy.resolve_merge_strategy(Path(tmp)), "squash")


if __name__ == "__main__":
    unittest.main()
