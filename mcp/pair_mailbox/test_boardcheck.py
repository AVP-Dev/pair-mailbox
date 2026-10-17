#!/usr/bin/env python3
"""Tests for bin/boardcheck.py (round 106 slice E).

The machine leg for the board: a ticked row must be provable. These tests build
boards in a scratch state dir and run the check against a REAL git repo, because
the whole point is git truth — a mocked sha resolver would test nothing.

Run: python3 mcp/pair_mailbox/test_boardcheck.py
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin"))

import boardcheck  # noqa: E402


def _git(cwd: Path, *args: str) -> str:
    out = subprocess.run(["git", *args], cwd=str(cwd),
                         capture_output=True, text=True, timeout=30)
    return (out.stdout or "").strip()


class BoardCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.state = self.root / ".pair"
        self.state.mkdir(parents=True, exist_ok=True)
        _git(self.root, "init", "-q", "-b", "master")
        _git(self.root, "config", "user.email", "t@t.t")
        _git(self.root, "config", "user.name", "t")
        (self.root / "a.py").write_text("x\n", encoding="utf-8")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", "base")
        self.board = self.state / "plan.md"
        # boardcheck resolves git in ITS OWN repo; point it here via ROOT override
        self._orig_root = boardcheck.ROOT
        boardcheck.ROOT = self.root

    def tearDown(self) -> None:
        boardcheck.ROOT = self._orig_root
        self._tmp.cleanup()

    def _commit(self, name: str) -> str:
        (self.root / name).write_text(name + "\n", encoding="utf-8")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", name)
        return _git(self.root, "rev-parse", "HEAD")

    def _write_board(self, row: str) -> Path:
        self.board.write_text(
            "# board\n\n## X. Slice\n\n" + row + "\n\n## Invariants\n\n- x\n",
            encoding="utf-8")
        return self.board

    # -- what makes a row provable ----------------------------------------

    def test_delivered_sha_resolving_in_branch_is_clean(self) -> None:
        sha = self._commit("work.py")[:8]
        ok, problems = boardcheck.check(self._write_board("- state: [x] delivered " + sha))
        self.assertTrue(ok, problems)

    def test_unresolvable_sha_is_named(self) -> None:
        ok, problems = boardcheck.check(self._write_board("- state: [x] delivered deadbee"))
        self.assertFalse(ok)
        self.assertTrue(any("does not resolve" in p for p in problems))

    def test_claim_without_sha_is_named(self) -> None:
        """The round-104 drift shape: a delivered claim bound to no revision."""
        ok, problems = boardcheck.check(self._write_board("- state: [x] delivered"))
        self.assertFalse(ok)
        self.assertTrue(any("must bind a revision" in p for p in problems))

    def test_sha_outside_this_branch_is_named(self) -> None:
        """A row naming work that is not in this branch — the shape slice C hit
        when a merged-away branch left slice C's commit stranded on local master.
        """
        sha = self._commit("stranded.py")[:8]
        _git(self.root, "checkout", "-q", "-b", "other")
        _git(self.root, "reset", "-q", "--hard", "HEAD~1")
        ok, problems = boardcheck.check(self._write_board("- state: [x] delivered " + sha))
        self.assertFalse(ok)
        self.assertTrue(any("not an ancestor" in p for p in problems))

    def test_missing_artefact_is_named(self) -> None:
        sha = self._commit("work.py")[:8]
        row = f"- state: [x] delivered {sha} — `bin/not-a-tool` wired"
        ok, problems = boardcheck.check(self._write_board(row))
        self.assertFalse(ok)
        self.assertTrue(any("does not carry" in p for p in problems))

    def test_existing_artefact_passes(self) -> None:
        sha = self._commit("work.py")[:8]
        # the artefact must exist in the TREE the check runs against, so name a
        # file this scratch repo actually carries
        row = f"- state: [x] delivered {sha} — `a.py` wired"
        ok, problems = boardcheck.check(self._write_board(row))
        self.assertTrue(ok, problems)

    # -- states that carry no claim ---------------------------------------

    def test_in_work_and_refused_rows_are_not_checked(self) -> None:
        """Not every row makes a claim: `in-work` and `refused` carry no sha and
        must not be demanded to — the check targets CLAIMS, not every row."""
        for state in ("[ ] in-work", "[ ] refused", "[x] approved unbound"):
            ok, problems = boardcheck.check(self._write_board(f"- state: {state}"))
            # 'approved unbound' carries no sha, so it too is a claim without bind
            if state == "[x] approved unbound":
                self.assertFalse(ok)
            else:
                self.assertTrue(ok, problems)

    def test_no_state_line_is_named(self) -> None:
        self.board.write_text("# board\n\n## X. Slice\n\n- nothing here\n", encoding="utf-8")
        ok, problems = boardcheck.check(self.board)
        self.assertFalse(ok)
        self.assertTrue(any("not parseable" in p for p in problems))


if __name__ == "__main__":
    unittest.main(verbosity=2)
