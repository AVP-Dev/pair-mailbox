"""Regression tests for doctor recovery locking."""
from __future__ import annotations

import importlib.util
import fcntl
from importlib.machinery import SourceFileLoader
import json
import os
import select
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCTOR = ROOT / "bin" / "pairmcpdoctor"
SERVER = ROOT / "mcp" / "pair_mailbox" / "server.py"


def load_doctor():
    loader = SourceFileLoader("pairmcpdoctor", str(DOCTOR))
    spec = importlib.util.spec_from_loader("pairmcpdoctor", loader)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DoctorLockCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / ".pair"
        self.state.mkdir()
        self.project = Path(self.tmp.name) / "project"
        self.project.mkdir()
        (self.state / "state.json").write_text(json.dumps(
            {"round": 900, "mode": "fix", "writer": "testrole", "roles": {},
             "phase": "implement", "seq": 0}), encoding="utf-8")
        self.doctor = load_doctor()
        self.doctor.STATE = self.state
        self.doctor.ROOT = self.project

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def fake_tmux(self, output: str) -> tuple[Path, Path]:
        bindir = Path(self.tmp.name) / "bin"
        bindir.mkdir(exist_ok=True)
        fake = bindir / "tmux"
        log = Path(self.tmp.name) / "tmux.jsonl"
        fake.write_text(
            "#!/usr/bin/env python3\nimport json, sys\n"
            f"open({str(log)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
            f"print({output!r}, end='')\n", encoding="utf-8")
        fake.chmod(0o755)
        return fake, log

    def test_pane_selection_uses_project_root_and_socket(self) -> None:
        local = self.project / "src"
        local.mkdir()
        foreign = Path(self.tmp.name) / "foreign"
        foreign.mkdir()
        rows = (f"%foreign|foreign-session|0|codex|{foreign}\n"
                f"%local|local-session|0|codex|{local}\n")
        fake, log = self.fake_tmux(rows)
        env = os.environ.copy()
        os.environ.update(PATH=f"{fake.parent}{os.pathsep}{env.get('PATH', '')}",
                          PAIR_PANE_SOCKET="pair-doctor-test")
        try:
            self.assertEqual("%local", self.doctor.pane_of("codex"))
            calls = [json.loads(line) for line in log.read_text().splitlines()]
        finally:
            os.environ.clear()
            os.environ.update(env)
        self.assertEqual([["-L", "pair-doctor-test", "list-panes", "-a", "-F",
                           "#{pane_id}|#{session_name}|#{pane_index}|#{pane_current_command}|#{pane_current_path}" ]],
                         calls)

    def test_doctor_never_reconnects_to_a_foreign_only_pane(self) -> None:
        foreign = Path(self.tmp.name) / "foreign"
        foreign.mkdir()
        fake, log = self.fake_tmux(f"%foreign|other|0|codex|{foreign}\n")
        env = os.environ.copy()
        os.environ.update(PATH=f"{fake.parent}{os.pathsep}{env.get('PATH', '')}",
                          PAIR_PANE_SOCKET="pair-doctor-test")
        argv = sys.argv
        sys.argv = [str(DOCTOR), "--role", "codex"]
        try:
            self.assertEqual(0, self.doctor.main())
            calls = [json.loads(line) for line in log.read_text().splitlines()]
        finally:
            sys.argv = argv
            os.environ.clear()
            os.environ.update(env)
        self.assertEqual(1, len(calls))
        self.assertEqual("list-panes", calls[0][2])

    def test_server_pid_ignores_same_role_in_foreign_project(self) -> None:
        script = Path(self.tmp.name) / "server.py"
        script.write_text("import time; print('ready', flush=True); time.sleep(60)",
                          encoding="utf-8")
        foreign_state = Path(self.tmp.name) / "foreign" / ".pair"
        foreign_state.mkdir(parents=True)

        def start(state: Path) -> subprocess.Popen:
            return subprocess.Popen(
                [sys.executable, str(script), "--role", "codex", "--state", str(state)],
                cwd=str(self.project), stdout=subprocess.PIPE, text=True)

        foreign_proc = start(foreign_state)
        local_proc = start(self.state)
        try:
            self.assertEqual("ready", foreign_proc.stdout.readline().strip())
            self.assertEqual("ready", local_proc.stdout.readline().strip())
            self.assertEqual(str(local_proc.pid), self.doctor.server_pid("codex"))
        finally:
            for proc in (foreign_proc, local_proc):
                proc.terminate()
                proc.wait(timeout=5)
                proc.stdout.close()

    def test_doctors_are_single_flight_per_role(self) -> None:
        first = self.doctor.role_lock("testrole")
        self.assertIsNotNone(first)
        try:
            self.assertIsNone(self.doctor.role_lock("testrole"))
        finally:
            self.doctor.fcntl.flock(first.fileno(), self.doctor.fcntl.LOCK_UN)
            first.close()

    def test_reconnect_timeout_does_not_type_filter_or_toggle(self) -> None:
        sends = []
        self.doctor.send = lambda pane, text, enter=False: sends.append((text, enter))
        self.doctor.wait_for_condition = lambda predicate, **kwargs: False
        outcome = self.doctor.reconnect("codex", "%1")
        self.assertIn("picker never appeared", outcome)
        self.assertEqual([("/mcp", True)], sends)

    def test_reconnect_waits_are_bounded_and_not_fixed_sleeps(self) -> None:
        source = DOCTOR.read_text(encoding="utf-8")
        self.assertNotIn("time.sleep(", source)
        self.assertIn("timeout=PICKER_TIMEOUT", source)
        self.assertIn("timeout=FILTER_TIMEOUT", source)
        self.assertIn("timeout=HANDSHAKE_TIMEOUT", source)

    def test_wrapped_connected_row_uses_logical_capture(self) -> None:
        calls = []
        self.doctor.tmux = lambda *args: (calls.append(args) or
                                           (0, "pair — Connected\n"))
        self.assertEqual("connected", self.doctor.pair_row_state("%1"))
        self.assertEqual(("capture-pane", "-t", "%1", "-p", "-J"), calls[0])

    def test_live_server_is_left_untouched(self) -> None:
        fake, log = self.fake_tmux("")
        env = os.environ.copy()
        os.environ.update(PATH=f"{fake.parent}{os.pathsep}{env.get('PATH', '')}")
        argv = sys.argv
        sys.argv = [str(DOCTOR), "--role", "codex"]
        self.doctor.server_pid = lambda role: "12345"
        try:
            self.assertEqual(0, self.doctor.main())
            calls = [json.loads(line) for line in log.read_text().splitlines()]
        finally:
            sys.argv = argv
            os.environ.clear()
            os.environ.update(env)
        self.assertEqual(1, len(calls))
        self.assertEqual("list-panes", calls[0][0])

    def test_replacement_server_starts_while_doctor_lock_is_held(self) -> None:
        doctor_lock = self.doctor.role_lock("testrole")
        self.assertIsNotNone(doctor_lock)
        server_lock_path = self.state / ".server-testrole.lock"
        proc = subprocess.Popen(
            [sys.executable, str(SERVER), "--role", "testrole", "--state", str(self.state)],
            cwd=str(ROOT), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        try:
            ready, _, _ = select.select([proc.stderr], [], [], 5)
            self.assertTrue(ready, "replacement server did not report startup")
            line = proc.stderr.readline()
            self.assertIn("ready role=testrole", line,
                          f"replacement server refused startup: {line}")
            handle = server_lock_path.open("a+")
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                handle.close()
        finally:
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate(timeout=10)
            self.doctor.fcntl.flock(doctor_lock.fileno(), self.doctor.fcntl.LOCK_UN)
            doctor_lock.close()

    def test_doctor_lock_is_released_when_process_dies(self) -> None:
        script = (
            "import sys,time,importlib.util; from importlib.machinery import SourceFileLoader; "
            "from pathlib import Path; "
            f"sys.path.insert(0, {str(ROOT / 'bin')!r}); "
            f"loader=SourceFileLoader('pairmcpdoctor', {str(DOCTOR)!r}); "
            "spec=importlib.util.spec_from_loader('pairmcpdoctor',loader); "
            "d=importlib.util.module_from_spec(spec); loader.exec_module(d); "
            f"d.STATE=Path({str(self.state)!r}); "
            "h=d.role_lock('testrole'); print('locked', flush=True); time.sleep(60)"
        )
        proc = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(proc.stdout.readline().strip(), "locked")
        finally:
            proc.kill()
            proc.wait(timeout=10)
            proc.stdout.close()
            proc.stderr.close()
        released = self.doctor.role_lock("testrole")
        self.assertIsNotNone(released)
        self.doctor.fcntl.flock(released.fileno(), self.doctor.fcntl.LOCK_UN)
        released.close()


if __name__ == "__main__":
    unittest.main()
