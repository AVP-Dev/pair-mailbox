"""The board gate must run from the installation, not from the project.

Round 127. The gate lived in `server.py::_done_blockers`, which reached the
checker by putting the PROJECT's `bin/` on `sys.path` and importing by name.
Three defects in four lines, all measured on a live tree:

  1. `sys.path` grew by one entry on every call, without bound;
  2. the import found the PROJECT's `boardcheck.py`, so the gate was dead in
     every deployment that is not the tool's own repository - it logged
     "board check skipped" and carried on;
  3. the same path executed project-supplied code inside the long-lived
     server process, on a security-relevant gate.

So the checker is loaded from THIS installation by path, and the check runs
against the PROJECT's tree - the tree the board's claims are about.

Run: python3 mcp/pair_mailbox/test_boardcheck_isolation.py
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pair_test_base import PairTestCase  # noqa: E402
from server import PairSession, Store  # noqa: E402


class TestBoardCheckIsolation(PairTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.allow_panes()

    def test_the_checker_comes_from_the_installation(self) -> None:
        """The gate runs the checker this installation ships, whatever the
        project carries."""
        import server

        checker = server._board_checker()
        self.assertIsNotNone(checker, "the shipped boardcheck must load")
        here = Path(server.__file__).resolve().parent
        self.assertEqual(Path(checker.__file__).resolve(),
                         (here.parent.parent / "bin" / "boardcheck.py").resolve(),
                         "the board gate must use the installation's checker")

    def test_a_project_without_a_boardcheck_still_gets_the_gate(self) -> None:
        """The defect this fixes: a project with no `bin/boardcheck.py` — which
        is every project but this one — had the gate skipped with a log line
        nobody reads. The checker comes from the installation, so the gate
        runs."""
        import server

        self.assertFalse((self.store.project_root / "bin" / "boardcheck.py").exists(),
                         "this test needs a project WITHOUT its own boardcheck")
        # A board whose row claims a sha that does not resolve: the gate must
        # find it and refuse.
        board = self.store.root / "plan.md"
        board.write_text(
            "# board\n\n## A. Slice\n\n- state: [x] delivered deadbee\n",
            encoding="utf-8")
        blockers = self.writer._done_blockers(self.store.read())
        self.assertTrue(
            any("the board claims what the tree cannot show" in b for b in blockers),
            f"the board gate must run in a project without its own boardcheck; "
            f"blockers: {blockers}")
        self.assertTrue(any("does not resolve" in b for b in blockers),
                        f"the gate must name the unresolvable sha; blockers: {blockers}")

    def test_sys_path_does_not_grow(self) -> None:
        """The old code inserted the project's bin/ on every call.

        A board has to be present: the old insert sat inside the `if
        board.exists()` branch, so without one the path was never touched
        and the growth hid.
        """
        (self.store.root / "plan.md").write_text(
            "# board\n\n## A. Slice\n\n- state: [ ] in-work\n", encoding="utf-8")
        before = list(sys.path)
        for _ in range(5):
            self.writer._done_blockers(self.store.read())
        self.assertEqual(len(sys.path), len(before),
                         f"sys.path grew: {len(before)} -> {len(sys.path)}")
        self.assertEqual(sys.path, before, "sys.path changed content")

    def test_project_supplied_boardcheck_is_not_executed(self) -> None:
        """The execution channel: a `bin/boardcheck.py` in the project must not
        run inside the server process."""
        marker = self.store.project_root / "EXECUTED_BY_SERVER"
        planted = self.store.project_root / "bin" / "boardcheck.py"
        planted.parent.mkdir(parents=True, exist_ok=True)
        planted.write_text(
            "import pathlib\n"
            f"pathlib.Path({str(marker)!r}).write_text('executed')\n"
            "def check(board, require_ancestor=True, root=None):\n"
            "    return True, []\n",
            encoding="utf-8")

        import server

        # The loader resolves once per process; drop the memo so this test
        # observes a fresh load with the plant in place.
        server._BOARD_CHECKER = None
        server._BOARD_CHECKER_TRIED = False
        try:
            checker = server._board_checker()
            self.assertIsNotNone(checker)
            self.assertNotEqual(Path(checker.__file__).resolve(),
                                planted.resolve(),
                                "the gate must not load the project's boardcheck")
            self.assertFalse(marker.exists(),
                             "the project's boardcheck.py was executed by the server")
        finally:
            server._BOARD_CHECKER = None
            server._BOARD_CHECKER_TRIED = False


if __name__ == "__main__":
    unittest.main(verbosity=2)
