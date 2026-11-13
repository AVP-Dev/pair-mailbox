#!/usr/bin/env python3
"""`bin/pairmcprestart`: the reconnect waits on a condition, not on a guess.

The defect this fixes: the command slept 6s for the picker, 2s for the filter
and up to 30s for the handshake — blind, and wrong in both directions. Too short
and it read a stale frame and typed into a pane that was not ready; too long
and every attempt cost 38 seconds even when the picker was up in 300ms.

The checks here are about the WAIT, not about the reconnect itself: the command
must not sleep a fixed time, it must ask a predicate and stop the moment the
predicate holds. The reconnect flow needs a live CLI in a pane, so it is not
reproduced here — the stubbed pieces prove the shape of the waits, and the
predicate helper itself is checked in `mcp/pair_mailbox/test_wait_until.py`.

Run: python3 bin/pairmcprestart_test.py
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "bin" / "pairmcprestart"


def _load_module() -> dict:
    """Load the script's functions without running its main().

    The script runs argparse at import time, so the file is parsed and its
    top-level assignments are replayed in a fresh namespace. The package path
    is set up first: the script imports the shared wait helper from it.
    """
    import sys

    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    keep = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.Import, ast.ImportFrom)):
            keep.append(node)
        elif isinstance(node, ast.Assign):
            keep.append(node)
    namespace: dict = {"__name__": "pairmcprestart_under_test", "__file__": str(SCRIPT),
                       "sys": sys}
    sys.path.insert(0, str(ROOT / "mcp" / "pair_mailbox"))
    try:
        exec(compile(ast.Module(body=keep, type_ignores=[]), "pairmcprestart", "exec"),
             namespace)
    finally:
        sys.path.remove(str(ROOT / "mcp" / "pair_mailbox"))
    return namespace


class TestNoBlindSleeps(unittest.TestCase):
    """A fixed sleep is a guess about another program's timing."""

    def setUp(self) -> None:
        self.source = SCRIPT.read_text(encoding="utf-8")
        self.module = _load_module()

    def test_the_script_never_sleeps_a_fixed_time(self) -> None:
        """A fixed sleep is a guess about another program's timing."""
        for needle in ("time.sleep(", '["sleep"', '"sleep",'):
            self.assertNotIn(needle, self.source,
                             f"the reconnect still waits a fixed time ({needle})")

    def test_it_imports_the_shared_wait_helper(self) -> None:
        """One wait, one place: the helper lives beside the other modules."""
        self.assertIn("wait_for_condition", self.module)

    def test_every_wait_has_a_deadline(self) -> None:
        """A poll without a ceiling never ends."""
        import re

        for match in re.finditer(r"wait_for_condition\(", self.source):
            tail = self.source[match.end():match.end() + 200]
            self.assertIn("timeout=", tail,
                          f"a wait without a deadline: {tail[:80]!r}")


class TestPickerDetection(unittest.TestCase):
    """The picker must be RECOGNISED, not assumed.

    The old code sent `/mcp`, slept, and typed "pair" into whatever was on
    screen. If the picker had not drawn yet, the filter text landed in the
    CLI's own prompt and the reconnect failed in a way nobody could read.
    """

    def setUp(self) -> None:
        self.module = _load_module()

    def test_the_picker_is_recognised_by_its_own_chrome(self) -> None:
        module = self.module
        original = module["_screen"]
        try:
            module["_screen"] = lambda pane: "MCP servers\n› context7\n› pair"
            self.assertTrue(module["_picker_open"]("%1"))
            module["_screen"] = lambda pane: "user@host:~$ "
            self.assertFalse(module["_picker_open"]("%1"))
        finally:
            module["_screen"] = original

    def test_the_pair_row_is_recognised(self) -> None:
        module = self.module
        original = module["_screen"]
        try:
            module["_screen"] = lambda pane: "› pair  connected"
            self.assertTrue(module["_pair_row_visible"]("%1"))
            module["_screen"] = lambda pane: "› context7"
            self.assertFalse(module["_pair_row_visible"]("%1"))
        finally:
            module["_screen"] = original

    def test_an_unreadable_pane_is_not_a_picker(self) -> None:
        module = self.module
        original = module["_screen"]
        try:
            module["_screen"] = lambda pane: ""
            self.assertFalse(module["_picker_open"]("%1"))
            self.assertFalse(module["_pair_row_visible"]("%1"))
        finally:
            module["_screen"] = original


class TestDeadlines(unittest.TestCase):
    def setUp(self) -> None:
        self.module = _load_module()

    def test_the_deadlines_are_positive_and_ordered(self) -> None:
        module = self.module
        self.assertGreater(module["PICKER_TIMEOUT"], 0)
        self.assertGreater(module["FILTER_TIMEOUT"], 0)
        self.assertGreater(module["HANDSHAKE_TIMEOUT"], 0)
        # The handshake is the slowest step: the server process only appears
        # after the toggle lands.
        self.assertGreater(module["HANDSHAKE_TIMEOUT"], module["PICKER_TIMEOUT"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
