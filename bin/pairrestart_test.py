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
                 mock.patch.object(PR, "runtime_code_mtime", return_value=1.0), \
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
                 mock.patch.object(PR, "runtime_code_mtime", return_value=9e9), \
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
                 mock.patch.object(PR, "runtime_code_mtime", return_value=9e9), \
                 mock.patch.object(PR, "pilot_procs", return_value=[]), \
                 mock.patch.object(PR, "act") as do_act:
                code = PR.main(["--state", str(sd), "--role", "omp", "--dry-run"])
        self.assertEqual(code, 0)
        do_act.assert_not_called()

    def test_bare_invocation_without_a_role_refuses(self) -> None:
        """Slice A: `pairrestart` used to kill EVERY stale server in the room. Now
        a bare invocation with no resolvable role refuses rc=2 and names the
        flag, the pane route, and --room. Tested with the env cleared so it
        cannot lean on the running pair's PAIR_ROLE/TMUX_PANE (that leak is what
        let it pass locally while CI failed)."""
        import os
        with tempfile.TemporaryDirectory() as tmp:
            sd = self._state(Path(tmp), ["omp"])
            with mock.patch.object(PR, "live_servers",
                                   return_value=[{"pid": 4242, "role": "omp", "start": "x"}]), \
                 mock.patch.object(PR, "proc_start_ts", return_value=1.0), \
                 mock.patch.object(PR, "runtime_code_mtime", return_value=9e9), \
                 mock.patch.object(PR, "pilot_procs", return_value=[]), \
                 mock.patch.dict(os.environ, {}, clear=True):
                code = PR.main(["--state", str(sd), "--dry-run"])
        self.assertEqual(code, 2, "a room-wide kill must never be the default")

    def test_abort_changes_nothing(self) -> None:
        """Anything but explicit 'yes' aborts without acting."""
        with tempfile.TemporaryDirectory() as tmp:
            sd = self._state(Path(tmp), ["omp"])
            with mock.patch.object(PR, "live_servers",
                                   return_value=[{"pid": 4242, "role": "omp", "start": "x"}]), \
                 mock.patch.object(PR, "proc_start_ts", return_value=1.0), \
                 mock.patch.object(PR, "runtime_code_mtime", return_value=9e9), \
                 mock.patch.object(PR, "pilot_procs", return_value=[]), \
                 mock.patch.object(PR, "act") as do_act, \
                 mock.patch("builtins.input", return_value="y"):
                code = PR.main(["--state", str(sd), "--role", "omp"])
                do_act.assert_not_called()

    # --- round 104 S0: the inventory belongs to one state dir ---------------

    _PS_LINE = ("  1234 Sat Oct 17 15:00:00 2026  900 /usr/bin/python3 "
                "/home/alubuntu/x/mcp/pair_mailbox/server.py --role {role} "
                "--state {state}")

    def test_foreign_state_server_is_not_our_inventory(self) -> None:
        """A server of ANOTHER project is not a row here, and never a kill target.

        Live proof (critic seq 1536): galatour's `--role omp` server was
        inventoried as this repo's FRESH omp and printed "Nothing stale" while
        our own stale omp stayed alive. A `--yes` run under that inventory kills
        another project's server — fixing one thing must not break another.
        """
        with tempfile.TemporaryDirectory() as tmp:
            mine = Path(tmp)
            foreign = mine.parent / "some-other-project"
            lines = [self._PS_LINE.format(role="omp", state=foreign),
                     self._PS_LINE.format(role="omp", state=mine)]
            with mock.patch.object(PR.subprocess, "run",
                                   return_value=mock.Mock(stdout="\n".join(lines))):
                found = PR.live_servers(mine)
        self.assertEqual([s["pid"] for s in found], [1234],
                         "only our own state's server may be inventoried")
        self.assertFalse(any("other-project" in str(s) for s in found))

    def test_state_equals_form_parses_too(self) -> None:
        """`--state=DIR` is the same membership as `--state DIR`."""
        with tempfile.TemporaryDirectory() as tmp:
            mine = Path(tmp)
            line = ("  99 Sat Oct 17 15:00:00 2026  900 /usr/bin/python3 "
                    f"/x/mcp/pair_mailbox/server.py --role omp --state={mine}")
            with mock.patch.object(PR.subprocess, "run",
                                   return_value=mock.Mock(stdout=line)):
                found = PR.live_servers(mine)
        self.assertEqual([s["pid"] for s in found], [99])

    def test_two_servers_of_one_role_both_show(self) -> None:
        """Two live servers of the SAME role and state: two rows, never one.

        diagnose() used to build a dict by role, so the last line silently won and
        a second same-role server looked FRESH while the real one sat stale.
        """
        with tempfile.TemporaryDirectory() as tmp:
            sd = self._state(Path(tmp), ["omp"])
            two = [{"pid": 11, "role": "omp", "start": "x"},
                   {"pid": 22, "role": "omp", "start": "x"}]
            with mock.patch.object(PR, "live_servers", return_value=two), \
                 mock.patch.object(PR, "proc_start_ts", return_value=1.0), \
                 mock.patch.object(PR, "runtime_code_mtime", return_value=9e9), \
                 mock.patch.object(PR, "pilot_procs", return_value=[]):
                rows = PR.diagnose(sd)
        pids = sorted(r["pid"] for r in rows if r["kind"] == "role")
        self.assertEqual(pids, [11, 22], "both same-role servers must be listed")
        self.assertEqual({r["verdict"] for r in rows if r["kind"] == "role"}, {"STALE"})

    def test_no_state_arg_means_not_ours(self) -> None:
        """A server launched without --state runs on its cwd: not ours to claim."""
        with tempfile.TemporaryDirectory() as tmp:
            mine = Path(tmp)
            line = ("  55 Sat Oct 17 15:00:00 2026  900 /usr/bin/python3 "
                    "/x/mcp/pair_mailbox/server.py --role omp")
            with mock.patch.object(PR.subprocess, "run",
                                   return_value=mock.Mock(stdout=line)):
                found = PR.live_servers(mine)
        self.assertEqual(found, [])




if __name__ == "__main__":
    unittest.main(verbosity=2)
