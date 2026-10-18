#!/usr/bin/env python3
"""Protocol tests: the round_base pin and the origin-truth caller — state written at round open, checked at close."""

from __future__ import annotations

import contextlib
import ast
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
from shutil import which as shutil_which

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pair_test_base import PAIRCHAT, SERVER, PairTestCase  # noqa: E402
from pairctl_read import project_row, render_table  # noqa: E402
from server import (  # noqa: E402
    AUTONOMIES,
    MAX_READ_WAIT_SECONDS,
    NOTE_LINE_CAP,
    NOTE_READ_MAX,
    PairSession,
    ProtocolError,
    Store,
    done_blockers,
    main,
    resolve_autonomy,
)

# The digest ceiling duplicates on purpose: the test must fall when the server
# raises its own constant above the checked one, not quietly rewrite it to match.
DIGEST_TOKEN_BUDGET_FOR_TEST = 500

def fresh_seen() -> str:
    """A fresh timestamp: the seed knowingly younger than any expiry TTL.

    Needed by the seeds of stale panes/dead processes: their intent is "drop the
    pane but keep the record", while year-old last_seen now honestly expires on
    TTL (19.3). Age runs random there; freshness pins the subject.
    """
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class TestRoundBasePin(PairTestCase):
    """Round 105 slice 2b prerequisite: the number round 104 lacked.

    The delivery floor answered `delivered` from local remote-tracking refs while
    origin sat 7 ahead — with no recorded base, "ahead of what?" had no answer.
    round_base is that answer: the head origin/master carries, stamped when the
    round opens.
    """

    @staticmethod
    def git(cwd: Path, *args: str) -> None:
        subprocess.run(["git", *args], cwd=str(cwd), check=True,
                       capture_output=True, text=True, timeout=10)

    def _fresh(self) -> tuple[Any, Any]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        proj = Path(tmp.name) / "proj"
        # Store.project_root is root.parent when the dir is named .pair — the
        # same derivation the server uses, so the fixture must sit in one.
        store = Store(proj / ".pair")
        return store, proj

    def test_round_open_stamps_the_head_origin_carries(self) -> None:
        store, proj = self._fresh()
        self.git(proj, "init", "-q")
        self.git(proj, "config", "user.email", "t@t.t")
        self.git(proj, "config", "user.name", "t")
        (proj / "a.py").write_text("x\n", encoding="utf-8")
        self.git(proj, "add", "-A")
        self.git(proj, "commit", "-qm", "base")
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(proj),
                              capture_output=True, text=True).stdout.strip()
        self.git(proj, "update-ref", "refs/remotes/origin/master", head)
        session = PairSession("omp", store)
        self.allow_panes()
        session.tool_pair_claim(reason="test")
        state = store.read()
        self.assertEqual(state.get("round_base"), head)

    def test_no_origin_leaves_base_unset_not_invented(self) -> None:
        """A repo with no origin/master must not fabricate a base: the comparison
        then reports its own inability, and never diffs against a guess."""
        store, proj = self._fresh()
        self.git(proj, "init", "-q")
        self.git(proj, "config", "user.email", "t@t.t")
        self.git(proj, "config", "user.name", "t")
        (proj / "a.py").write_text("x\n", encoding="utf-8")
        self.git(proj, "add", "-A")
        self.git(proj, "commit", "-qm", "base")
        session = PairSession("omp", store)
        self.allow_panes()
        session.tool_pair_claim(reason="test")
        self.assertIsNone(store.read().get("round_base"))

    def test_base_is_not_restamped_inside_one_round(self) -> None:
        """The base belongs to the round that opened it: a reopen within the same
        round must not move it, or the comparison would diff against the wrong end."""
        store, proj = self._fresh()
        self.git(proj, "init", "-q")
        self.git(proj, "config", "user.email", "t@t.t")
        self.git(proj, "config", "user.name", "t")
        (proj / "a.py").write_text("x\n", encoding="utf-8")
        self.git(proj, "add", "-A")
        self.git(proj, "commit", "-qm", "base")
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(proj),
                              capture_output=True, text=True).stdout.strip()
        self.git(proj, "update-ref", "refs/remotes/origin/master", head)
        session = PairSession("omp", store)
        self.allow_panes()
        session.tool_pair_claim(reason="test")
        first = store.read()["round_base"]
        # origin/master moves mid-round: the pinned base must hold
        (proj / "b.py").write_text("y\n", encoding="utf-8")
        self.git(proj, "add", "-A")
        self.git(proj, "commit", "-qm", "second")
        moved = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(proj),
                               capture_output=True, text=True).stdout.strip()
        self.git(proj, "update-ref", "refs/remotes/origin/master", moved)
        store.mutate(lambda s: s.update({"round": s.get("round", 0) + 1}))
        self.assertEqual(store.read().get("round_base"), first)
        self.assertNotEqual(first, moved)





class TestOriginTruthCaller(PairTestCase):
    """Round 105 slice 2b, the caller leg: a function nobody calls is not a slice.

    origin_truth is landed and tested (161e788); these tests pin that the CLOSE
    actually refuses on it — behind-by-N must BLOCK, not exist. Round 104's shape:
    the floor answered delivered while origin sat 7 ahead.
    """

    @staticmethod
    def git(cwd: Path, *args: str) -> None:
        subprocess.run(["git", *args], cwd=str(cwd), check=True,
                       capture_output=True, text=True, timeout=10)

    @staticmethod
    def rev(cwd: Path, ref: str) -> str:
        return subprocess.run(["git", "rev-parse", ref], cwd=str(cwd),
                              capture_output=True, text=True, timeout=10).stdout.strip()

    def _repo(self, tmp: Path) -> Path:
        proj = tmp / "proj"
        proj.mkdir()
        self.git(proj, "init", "-q", "-b", "master")
        self.git(proj, "config", "user.email", "t@t.t")
        self.git(proj, "config", "user.name", "t")
        (proj / "a.py").write_text("x\n", encoding="utf-8")
        self.git(proj, "add", "-A")
        self.git(proj, "commit", "-qm", "base")
        self.git(proj, "remote", "add", "origin", "https://github.com/o/r.git")
        self.git(proj, "update-ref", "refs/remotes/origin/master", self.rev(proj, "HEAD"))
        return proj

    def _session(self, tmp: Path, proj: Path) -> tuple[Any, Any]:
        """A session whose state dir sits OUTSIDE the git tree.

        The repo under test switches branches, and `git checkout` deletes an
        untracked `.pair/state.json` in the way — which removes the very
        round_base this test checks, reading as "no base" instead of "behind".
        Store.project_root is a read-only property, so the override is pinned on
        the class for the duration of the test, not on the instance.
        """
        store = Store(tmp / "state")
        session = PairSession("omp", store)
        from unittest import mock as _m
        patcher = _m.patch.object(type(store), "project_root", property(lambda self: proj))
        patcher.start()
        self.addCleanup(patcher.stop)
        return store, session

    def test_close_refuses_when_local_is_behind_origin(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            proj = self._repo(Path(tmp))
            store, session = self._session(Path(tmp), proj)
            self.allow_panes()
            session.tool_pair_claim(reason="test")
            base = store.read().get("round_base")
            self.assertTrue(base, "fixture must have a pinned round_base")
            # origin advances (someone else merged), local stays at base —
            # the round-104 shape: floor would answer `delivered` here.
            # NB: no `reset --hard` in this fixture — it deletes the untracked
            # .pair/state.json under test, which reads as "no round_base" and
            # hides the very blocker being checked.
            self.git(proj, "checkout", "-q", "-b", "elsewhere")
            (proj / "b.py").write_text("y\n", encoding="utf-8")
            self.git(proj, "add", "-A")
            self.git(proj, "commit", "-qm", "origin-only")
            self.git(proj, "update-ref", "refs/remotes/origin/master", self.rev(proj, "HEAD"))
            self.git(proj, "checkout", "-q", "master")
            self.assertEqual(self.rev(proj, "HEAD"), base, "fixture: local must sit at base")
            self.assertIn("round_base", (Path(tmp) / "state" / "state.json").read_text(encoding="utf-8"),
                          "fixture guard: the state file must survive the git work")
            stale = store.read()
            blockers = session._done_blockers(stale)
            self.assertTrue(any("not against current origin" in b for b in blockers),
                            f"behind must block the close; blockers were: {blockers}")
            self.assertTrue(any("behind origin/master" in b for b in blockers))

    def test_close_passes_when_local_matches_origin(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            proj = self._repo(Path(tmp))
            store, session = self._session(Path(tmp), proj)
            self.allow_panes()
            session.tool_pair_claim(reason="test")
            blockers = session._done_blockers(store.read())
            self.assertFalse([b for b in blockers if "origin" in b],
                             f"aligned must not block; blockers: {blockers}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
