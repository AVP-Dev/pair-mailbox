#!/usr/bin/env python3
"""Tests for pairrestart's per-role scope (round 106 slice A).

Written before the fix, because the fix is a scope rule and a test that cannot
fail yet proves nothing. The behaviour under test is narrow and load-bearing: my
hygiene must not be your outage.

Run: python3 bin/pairrestart_scope_test.py
"""
from __future__ import annotations

import importlib.util as _ilu
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load():
    from importlib.machinery import SourceFileLoader
    loader = SourceFileLoader("pairrestart_mod", str(ROOT / "bin" / "pairrestart"))
    spec = _ilu.spec_from_loader("pairrestart_mod", loader)
    mod = _ilu.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


PR = _load()


class TestRoleScope(unittest.TestCase):
    """Which servers a restart is allowed to kill."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.sd = self.tmp / ".pair"
        self.sd.mkdir(parents=True, exist_ok=True)
        (self.sd / "state.json").write_text(
            json.dumps({"roles": {"omp": {}, "opencode": {}, "pi": {}},
                        "writer": "omp"}), encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _rows(self, *names: str, stale_for: set[str] | None = None) -> list[dict]:
        rows = []
        for i, name in enumerate(names):
            pid = 100000 + i
            rows.append({"kind": "role", "name": name, "pid": pid,
                         "verdict": "STALE" if not stale_for or name in stale_for else "FRESH",
                         "started": "10-17 12:00", "code": "10-17 12:05",
                         "detail": "kill, session reconnects"})
        return rows

    # -- default scope ----------------------------------------------------

    def test_default_scope_is_my_role_only(self) -> None:
        """The defect being fixed: `pairrestart` used to kill EVERY stale server
        in the state dir, so my test-commit restart was the peer's outage.
        Default scope must now be the invoking role alone."""
        rows = self._rows("omp", "opencode", "pi")
        # the caller resolves through PANE/PAIR_ROLE; with neither present the
        # honest answer is EMPTY — a default is a guess, not a scope. An explicit
        # empty env is passed because a test must not read the ambient one (the
        # running pair's own PAIR_ROLE would otherwise answer for it).
        self.assertEqual(PR.scope_rows(rows, state_dir=self.sd, env={}), [])
        self.assertEqual(
            [r["name"] for r in PR.scope_rows(rows, state_dir=self.sd,
                                              env={"PAIR_ROLE": "omp"})],
            ["omp"])

    def test_room_wide_names_every_role_before_any_kill(self) -> None:
        """A room-wide restart is legitimate, but it must be LISTED, not
        inferred: the caller sees every role it will disturb."""
        rows = self._rows("omp", "opencode", "pi")
        self.assertEqual({r["name"] for r in
                          PR.scope_rows(rows, state_dir=self.sd, room=True)},
                         {"omp", "opencode", "pi"})

    def test_explicit_role_targets_that_role(self) -> None:
        rows = self._rows("omp", "opencode")
        self.assertEqual(
            [r["name"] for r in PR.scope_rows(rows, role="opencode", state_dir=self.sd)],
            ["opencode"])

    # -- what the scope protects ------------------------------------------

    def test_fresh_servers_are_never_in_scope(self) -> None:
        """STALE means 'kill, session reconnects'; FRESH must never be touched,
        even in a room-wide run — a fresh server is not mine to disturb."""
        rows = self._rows("omp", "opencode", stale_for={"omp"})
        self.assertEqual([r["name"] for r in
                          PR.scope_rows(rows, state_dir=self.sd, room=True)], ["omp"])
        self.assertEqual(PR.scope_rows(rows, role="opencode", state_dir=self.sd), [])

    def test_unknown_role_is_empty_not_everything(self) -> None:
        """Naming a role that has no server must not widen the scope: an empty
        answer, never 'fall back to everything'."""
        rows = self._rows("omp", "opencode")
        self.assertEqual(PR.scope_rows(rows, role="claude", state_dir=self.sd), [])

    def test_pane_resolution_never_guesses_the_writer(self) -> None:
        """The writer in state is a ROLE, not necessarily the caller. A pane
        pointing at another role must return THAT role, not the writer."""
        (self.sd / "state.json").write_text(json.dumps(
            {"roles": {"omp": {"pane": "%1"}, "opencode": {"pane": "%2"}},
             "writer": "opencode"}), encoding="utf-8")
        env = {"TMUX_PANE": "%1"}
        self.assertEqual(PR.caller_role(self.sd, env), "omp")
        env = {"TMUX_PANE": "1"}          # the % is optional in both spellings
        self.assertEqual(PR.caller_role(self.sd, env), "omp")
        env = {"TMUX_PANE": "%99"}        # a pane of no role: empty, not the writer
        self.assertEqual(PR.caller_role(self.sd, env), "")
        self.assertEqual(PR.caller_role(self.sd, {}), "")

    # -- mtime rider ------------------------------------------------------

    def test_code_mtime_ignores_test_files(self) -> None:
        """Rider (b): a commit that only touches tests must NOT make a peer's
        runtime server stale. The 24h outage pattern's root cause."""
        pkg = self.tmp / "mcp" / "pair_mailbox"
        pkg.mkdir(parents=True)
        now = 1_000_000_000.0
        for runtime in ("server.py", "serve.py", "review.py"):
            (pkg / runtime).write_text("x=1\n", encoding="utf-8")
        for test in ("test_server.py", "test_protocol.py"):
            (pkg / test).write_text("x=1\n", encoding="utf-8")
        import os
        for runtime in ("server.py", "serve.py", "review.py"):
            os.utime(pkg / runtime, (now - 500, now - 500))
        for test in ("test_server.py", "test_protocol.py"):
            os.utime(pkg / test, (now + 50, now + 50))
        newest = PR.runtime_code_mtime(self.tmp)
        self.assertIsNotNone(newest)
        self.assertLess(newest, now, "a test file must not extend the runtime mtime")


if __name__ == "__main__":
    unittest.main(verbosity=2)
