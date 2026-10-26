"""Unit tests for project_settings.py (round 148).

This module is the ONE place that knows the order a project's voice is
heard in: the environment, the local ignored file, the committed project
file, the default. A setting that invents its own order is a second way to
read a setting — and two ways to read a setting are two places to look
when a round refuses for the "wrong" reason.

So the thing worth proving is not that one setting resolves (its own test
file proves that), but that the ORDER is what it says it is, for every
setting, and that the order is the trunk's.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pair_test_base import PairTestCase
import project_settings
import trunk


class TestProjectSettings(PairTestCase):
    """One order, for every setting, the trunk's order."""

    def _root(self, tmp: str) -> Path:
        root = Path(tmp)
        (root / ".pair").mkdir()
        return root

    def test_the_environment_comes_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            (root / ".pair" / "thing").write_text("local\n", encoding="utf-8")
            (root / ".thing").write_text("committed\n", encoding="utf-8")
            with patch.dict(os.environ, {project_settings.env_var("thing"): "env"}):
                self.assertEqual(project_settings.resolve(root, "thing"), "env")

    def test_the_local_file_comes_before_the_committed_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            (root / ".pair" / "thing").write_text("local\n", encoding="utf-8")
            (root / ".thing").write_text("committed\n", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop(project_settings.env_var("thing"), None)
                self.assertEqual(project_settings.resolve(root, "thing"), "local")

    def test_the_committed_file_comes_before_the_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            (root / ".thing").write_text("committed\n", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop(project_settings.env_var("thing"), None)
                self.assertEqual(project_settings.resolve(root, "thing"), "committed")

    def test_the_default_is_the_last_word(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop(project_settings.env_var("thing"), None)
                self.assertEqual(project_settings.resolve(root, "thing", "fallback"),
                                 "fallback")

    def test_the_first_non_empty_line_is_the_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            (root / ".thing").write_text("\n\n  value  \n", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop(project_settings.env_var("thing"), None)
                self.assertEqual(project_settings.resolve(root, "thing"), "value")

    def test_a_missing_file_is_not_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop(project_settings.env_var("thing"), None)
                self.assertEqual(project_settings.resolve(root, "thing", "fallback"),
                                 "fallback")

    def test_an_unreadable_file_is_not_an_error(self) -> None:
        """A file that cannot be read is a file that says nothing."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".thing").mkdir()  # a DIRECTORY where a file belongs
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop(project_settings.env_var("thing"), None)
                self.assertEqual(project_settings.resolve(root, "thing", "fallback"),
                                 "fallback")

    def test_the_known_settings_have_their_own_names(self) -> None:
        """The names are spelled out rather than derived: the derivation
        would invent a second naming convention next to `PAIR_TRUNK`, and a
        project that already reads `PAIR_TRUNK` must not have to learn a
        different rule for the next setting."""
        self.assertEqual(project_settings.env_var("merge_strategy"),
                         "PAIR_MERGE_STRATEGY")
        self.assertEqual(project_settings.env_var("worktree-share"),
                         "PAIR_WORKTREE_SHARE")

    def test_an_unknown_setting_falls_back_to_the_prefix(self) -> None:
        """The fallback exists so a new setting needs no edit here."""
        self.assertEqual(project_settings.env_var("new-thing"), "PAIR_SETTING_NEW_THING")

    def test_the_local_file_lives_in_the_ignored_state_dir(self) -> None:
        """A local choice must never dirty the tree and never travel."""
        self.assertEqual(project_settings.local_file(Path("/p"), "thing"),
                         Path("/p/.pair/thing"))

    def test_the_committed_file_beside_the_code(self) -> None:
        self.assertEqual(project_settings.project_file(Path("/p"), "thing"),
                         Path("/p/.thing"))

    def test_the_list_setting_splits_and_drops_the_blanks(self) -> None:
        """A list is a setting, not a sentence: `.venv, node_modules` and
        `.venv,node_modules` are the same list."""
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            (root / ".thing").write_text(".venv, node_modules\n", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop(project_settings.env_var("thing"), None)
                self.assertEqual(
                    project_settings.resolve_list(root, "thing", ["d"]),
                    [".venv", "node_modules"])

    def test_an_empty_list_falls_back_to_the_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp)
            (root / ".thing").write_text(" , , \n", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop(project_settings.env_var("thing"), None)
                self.assertEqual(
                    project_settings.resolve_list(root, "thing", ["d"]), ["d"])

    def test_the_order_is_the_trunks_order(self) -> None:
        """The trunk's order, for the same reasons. The trunk reads the
        environment, then `.pair/trunk`, then `.pair-trunk`; the settings
        read the environment, then `.pair/<name>`, then `.<name>`. A project
        that learned one has learned the other."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".pair").mkdir()
            with patch.dict(os.environ, {"PAIR_TRUNK": "from-env"}):
                self.assertEqual(trunk.resolve_trunk(root), "from-env")
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("PAIR_TRUNK", None)
                (root / ".pair" / "trunk").write_text("local-trunk\n", encoding="utf-8")
                self.assertEqual(trunk.resolve_trunk(root), "local-trunk")
                (root / ".pair-trunk").write_text("committed-trunk\n", encoding="utf-8")
                self.assertEqual(trunk.resolve_trunk(root), "local-trunk")
                (root / ".pair" / "trunk").unlink()
                self.assertEqual(trunk.resolve_trunk(root), "committed-trunk")


if __name__ == "__main__":
    unittest.main()
