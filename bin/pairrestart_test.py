#!/usr/bin/env python3
"""Tests for bin/pairrestart (round 92 C3).

Behavioural: stale plans kill, fresh plans spare, dry-run changes nothing.
Run: python3 bin/pairrestart_test.py
"""
from __future__ import annotations

import importlib.util as _ilu
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent


def _load():
    from importlib.machinery import SourceFileLoader
    loader = SourceFileLoader("pairrestart_mod", str(ROOT / "bin" / "pairrestart"))
    spec = _ilu.spec_from_loader("pairrestart_mod", loader)
    mod = _ilu.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


PR = _load()


class TestDiagnose(unittest.TestCase):
    def _state(self, tmp: Path, roles: list[str]) -> Path:
        sd = tmp / ".pair"
        sd.mkdir()
        (sd / "state.json").write_text(
            json.dumps({"roles": {r: {} for r in roles}}), encoding="utf-8")
        return sd

    def test_absent_server_needs_reconnect(self) -> None:
        """No live server for a role: ABSENT, never a kill target."""
        with tempfile.TemporaryDirectory() as tmp:
            sd = self._state(Path(tmp), ["omp"])
            with mock.patch.object(PR, "live_servers", return_value=[]), \
                 mock.patch.object(PR, "pilot_procs", return_value=[]):
                rows = PR.diagnose(sd)
        role_rows = [r for r in rows if r["kind"] == "role"]
        self.assertEqual(role_rows[0]["verdict"], "ABSENT")
        self.assertIsNone(role_rows[0]["pid"])

    def test_fresh_server_left_alone(self) -> None:
        """Server newer than code: FRESH, kill list stays empty."""
        with tempfile.TemporaryDirectory() as tmp:
            sd = self._state(Path(tmp), ["omp"])
            with mock.patch.object(PR, "live_servers",
                                   return_value=[{"pid": 1, "role": "omp", "start": "x"}]), \
                 mock.patch.object(PR, "proc_start_ts", return_value=9e9), \
                 mock.patch.object(PR, "server_code_mtime", return_value=1.0), \
                 mock.patch.object(PR, "pilot_procs", return_value=[]):
                rows = PR.diagnose(sd)
        self.assertEqual(rows[0]["verdict"], "FRESH")
        self.assertEqual(PR.print_plan(rows), 0)

    def test_stale_server_planned_for_kill(self) -> None:
        """Code newer than server start: STALE with a pid to kill."""
        with tempfile.TemporaryDirectory() as tmp:
            sd = self._state(Path(tmp), ["omp"])
            with mock.patch.object(PR, "live_servers",
                                   return_value=[{"pid": 4242, "role": "omp", "start": "x"}]), \
                 mock.patch.object(PR, "proc_start_ts", return_value=1.0), \
                 mock.patch.object(PR, "server_code_mtime", return_value=9e9), \
                 mock.patch.object(PR, "pilot_procs", return_value=[]):
                rows = PR.diagnose(sd)
        self.assertEqual(rows[0]["verdict"], "STALE")
        self.assertEqual(rows[0]["pid"], 4242)

    def test_dry_run_changes_nothing(self) -> None:
        """--dry-run prints the plan and kills nothing, exit 0."""
        with tempfile.TemporaryDirectory() as tmp:
            sd = self._state(Path(tmp), ["omp"])
            with mock.patch.object(PR, "live_servers",
                                   return_value=[{"pid": 4242, "role": "omp", "start": "x"}]), \
                 mock.patch.object(PR, "proc_start_ts", return_value=1.0), \
                 mock.patch.object(PR, "server_code_mtime", return_value=9e9), \
                 mock.patch.object(PR, "pilot_procs", return_value=[]), \
                 mock.patch.object(PR, "act") as do_act:
                code = PR.main(["--state", str(sd), "--dry-run"])
        self.assertEqual(code, 0)
        do_act.assert_not_called()

    def test_abort_changes_nothing(self) -> None:
        """Anything but explicit 'yes' aborts without acting."""
        with tempfile.TemporaryDirectory() as tmp:
            sd = self._state(Path(tmp), ["omp"])
            with mock.patch.object(PR, "live_servers",
                                   return_value=[{"pid": 4242, "role": "omp", "start": "x"}]), \
                 mock.patch.object(PR, "proc_start_ts", return_value=1.0), \
                 mock.patch.object(PR, "server_code_mtime", return_value=9e9), \
                 mock.patch.object(PR, "pilot_procs", return_value=[]), \
                 mock.patch.object(PR, "act") as do_act, \
                 mock.patch("builtins.input", return_value="y"):
                code = PR.main(["--state", str(sd)])
        self.assertEqual(code, 0)
        do_act.assert_not_called()




if __name__ == "__main__":
    unittest.main(verbosity=2)
