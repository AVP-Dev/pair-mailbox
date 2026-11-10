"""Regression tests for doctor recovery locking."""
from __future__ import annotations

import importlib.util
import fcntl
from importlib.machinery import SourceFileLoader
import json
import select
import subprocess
import sys
import tempfile
import unittest
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
        (self.state / "state.json").write_text(json.dumps(
            {"round": 900, "mode": "fix", "writer": "testrole", "roles": {},
             "phase": "implement", "seq": 0}), encoding="utf-8")
        self.doctor = load_doctor()
        self.doctor.STATE = self.state

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_doctors_are_single_flight_per_role(self) -> None:
        first = self.doctor.role_lock("testrole")
        self.assertIsNotNone(first)
        try:
            self.assertIsNone(self.doctor.role_lock("testrole"))
        finally:
            self.doctor.fcntl.flock(first.fileno(), self.doctor.fcntl.LOCK_UN)
            first.close()

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
