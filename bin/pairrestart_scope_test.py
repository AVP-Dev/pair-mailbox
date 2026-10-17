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
import subprocess
import sys
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

    def _state(self, tmp: Path, roles: dict) -> Path:
        sd = tmp / ".pair"
        sd.mkdir(parents=True, exist_ok=True)
        (sd / "state.json").write_text(json.dumps({"roles": roles, "writer": "omp"}),
                                       encoding="utf-8")
        return sd

    def _rows(self, *names: str, stale_for: set[str] | None = None) -> list[dict]:
        """Each role is STALE or FRESH, with a fake start behind it."""
        rows = []
        for name in names:
            pid = 100000 + len(rows)
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
        with tempfile.TemporaryDirectory() as tmp:
            self._state(Path(tmp), {"omp": {}, "opencode": {}})
            rows = self._rows("omp", "opencode", "pi")
            scope = PR.scope_rows(rows, role=None, writer="omp", room=False)
            self.assertEqual([r["name"] for r in scope], ["omp"])

    def test_room_wide_names_every_role_before_any_kill(self) -> None:
        """A room-wide restart is legitimate, but it must be LISTED, not
        inferred: the caller sees every role it will disturb."""
        with tempfile.TemporaryDirectory() as tmp:
            self._state(Path(tmp), {"omp": {}, "opencode": {}, "pi": {}})
            rows = self._rows("omp", "opencode", "pi")
            scope = PR.scope_rows(rows, role=None, writer="omp", room=True)
            self.assertEqual({r["name"] for r in scope}, {"omp", "opencode", "pi"})

    def test_explicit_role_targets_that_role(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._state(Path(tmp), {"omp": {}, "opencode": {}})
            rows = self._rows("omp", "opencode")
            scope = PR.scope_rows(rows, role="opencode", writer="omp", room=False)
            self.assertEqual([r["name"] for r in scope], ["opencode"])

    # -- what the scope protects ------------------------------------------

    def test_fresh_servers_are_never_in_scope(self) -> None:
        """STALE means 'kill, session reconnects'; FRESH must never be touched,
        even in a room-wide run — a fresh server is not mine to disturb."""
        with tempfile.TemporaryDirectory() as tmp:
            self._state(Path(tmp), {"omp": {}, "opencode": {}})
            rows = self._rows("omp", "opencode", stale_for={"omp"})
            self.assertEqual([r["name"] for r in PR.scope_rows(rows, writer="omp", room=True)],
                             ["omp"])
            self.assertEqual(PR.scope_rows(rows, role="opencode", writer="omp"), [])

    def test_unknown_role_is_empty_not_everything(self) -> None:
        """Naming a role that has no server must not widen the scope: an empty
        answer with no room flag, never 'fall back to everything'."""
        rows = self._rows("omp", "opencode")
        self.assertEqual(PR.scope_rows(rows, role="claude", writer="omp"), [])

    # -- mtime rider ------------------------------------------------------

    def test_code_mtime_ignores_test_files(self) -> None:
        """Rider (b): a commit that only touches tests must NOT make a peer's
        runtime server stale. The 24h outage pattern's root cause."""
        with tempfile.TemporaryDirectory() as tmp:
            pkg = Path(tmp) / "mcp" / "pair_mailbox" / "runtime" / "pair_mailbox"
            (pkg).mkdir(parents=True)
            (pkg / "server.py").write_text("x=1\n", encoding="utf-8")
            for test_name in ("test_server.py", "test_protocol.py"):
                (pkg.parent.parent / test_name).write_text("x=1\n", encoding="utf-8")
            # the runtime source is the OLDEST thing on disk
            import os, time as _t
            now = _t.time()
            os.utime(pkg / "server.py", (now - 500, now - 500))
            for other in [pkg / "serve.py", pkg / "review.py"]:
                other.write_text("y=2\n", encoding="utf-8")
                os.utime(other, (now - 500, now - 500))
            for test_name in ("test_server.py", "test_protocol.py"):
                os.utime(pkg.parent.parent / test_name, (now + 50, now + 50))
            newest = PR.runtime_code_mtime(tmp)
            self.assertIsNotNone(newest)
            self.assertLess(newest, now, "a test file must not extend the runtime mtime")


if __name__ == "__main__":
    unittest.main(verbosity=2)
