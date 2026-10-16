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


class TestRestartTrace(unittest.TestCase):
    """The durable trace: written before the kill, one line, both pids named.

    Round 101 cost a real channel-dark period: pairrestart killed two role
    servers and neither killed role ever learned it from the project. The
    trace is the cure — the reconnecting role reads restarts_last in
    pair_state.
    """

    def _state(self, tmp: Path, roles: list[str]) -> Path:
        sd = tmp / ".pair"
        sd.mkdir()
        (sd / "state.json").write_text(
            json.dumps({"roles": {r: {} for r in roles}}), encoding="utf-8")
        return sd

    def _stale_rows(self) -> list[dict]:
        return [
            {"kind": "role", "name": "omp", "pid": 4242, "verdict": "STALE"},
            {"kind": "role", "name": "opencode", "pid": 4243, "verdict": "STALE"},
            {"kind": "role", "name": "fresh", "pid": 4244, "verdict": "FRESH"},
            {"kind": "role", "name": "absent", "pid": None, "verdict": "ABSENT"},
        ]

    def test_trace_written_before_the_kill_names_both_pids(self) -> None:
        """The line lands, names every kill target, and spares the fresh."""
        with tempfile.TemporaryDirectory() as tmp:
            sd = self._state(Path(tmp), ["omp", "opencode"])
            kills: list[int] = []

            def fake_kill(pid: int, sig: object) -> None:
                kills.append(pid)
                trace = (sd / "restarts.jsonl").read_text(encoding="utf-8")
                # Written BEFORE the first kill of this run: the writer must
                # not die before its own record.
                self.assertIn("4242", trace)
                self.assertIn("4243", trace)

            with mock.patch.object(PR, "os") as fake_os, \
                 mock.patch.object(PR, "_pid_alive", return_value=False):
                fake_os.kill.side_effect = fake_kill
                fake_os.OSError = OSError
                PR.act(self._stale_rows(), sd)
            self.assertEqual(kills, [4242, 4243])
            # Inside the TemporaryDirectory scope: the state dir is the temp
            # one, gone by the time the with block exits.
            lines = (sd / "restarts.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            row = json.loads(lines[0])
            self.assertEqual(row["why"], "pairrestart")
            self.assertEqual([r["pid"] for r in row["roles"]], [4242, 4243])
            self.assertEqual([r["role"] for r in row["roles"]], ["omp", "opencode"])
            self.assertNotIn("fresh", json.dumps(row))

    def test_reader_returns_newest_and_survives_garbage(self) -> None:
        """restart_trace.restarts_last: newest dict wins, junk lines skipped."""
        sys.path.insert(0, str(ROOT / "mcp" / "pair_mailbox"))
        import restart_trace

        with tempfile.TemporaryDirectory() as tmp:
            sd = Path(tmp)
            self.assertIsNone(restart_trace.restarts_last(sd))
            (sd / "restarts.jsonl").write_text(
                "not json\n"
                + json.dumps({"ts": "t1", "roles": [{"role": "omp", "pid": 1}]}) + "\n"
                + json.dumps({"ts": "t2", "roles": [{"role": "opencode", "pid": 2}]}) + "\n",
                encoding="utf-8")
            last = restart_trace.restarts_last(sd)
        self.assertEqual(last["ts"], "t2")
        self.assertEqual(last["roles"][0]["pid"], 2)

    def test_statecache_sees_a_fresh_line(self) -> None:
        """A new restart line breaks the unchanged fingerprint stub."""
        sys.path.insert(0, str(ROOT / "mcp" / "pair_mailbox"))
        import statecache

        state = {"mode": "fix"}
        base = statecache.fingerprint(state, restart_line=None)
        after = statecache.fingerprint(
            state, restart_line={"ts": "t2", "roles": [{"role": "omp", "pid": 2}]})
        self.assertNotEqual(base, after)


if __name__ == "__main__":
    unittest.main(verbosity=2)
