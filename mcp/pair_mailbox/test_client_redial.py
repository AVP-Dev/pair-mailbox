#!/usr/bin/env python3
"""The thin client re-dials a dead daemon (round 113 slice 1).

The limitation found live while flipping the pair (round 112): the client dialled
ONCE at start, the daemon died, and the client sat `pair Failed` for the rest of
the session — reconnect granularity was the SESSION, not the event. Measured
twice on the live peer pane, with the daemon confirmed live and the role free.

A supervisor is NOT the answer (the client must never spawn, never manage a
role, never outlive its CLI). The answer is a BOUNDED re-dial: when the daemon
closes the connection, the client dials again for a short window, then reports
the same failure it reports today — with the same remedy.

These tests run REAL daemons on REAL sockets: kill the daemon, raise a new one,
and assert the client's next frame is served by the NEW daemon.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SERVER = ROOT / "mcp" / "pair_mailbox" / "server.py"
CLIENT = ROOT / "mcp" / "pair_mailbox" / "mcp_socket_client.py"
# Redial is opt-in per run so the default lane is unchanged: PAIR_SOCKET_REDIAL=1
REDIAL = "5"      # seconds, bounded — never an unbounded supervisor


class ClientRedialCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.proj = Path(self._tmp.name)
        (self.proj / ".pair").mkdir(parents=True)
        (self.proj / ".pair" / "state.json").write_text(json.dumps(
            {"round": 907, "mode": "fix", "writer": "omp", "roles": {},
             "phase": "implement", "seq": 0}), encoding="utf-8")
        self.sock = str(self.proj / ".pair" / ".server-redialrole.sock")
        self.procs: list[subprocess.Popen] = []

    def tearDown(self) -> None:
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
        self._tmp.cleanup()

    def _daemon(self) -> subprocess.Popen:
        proc = subprocess.Popen(
            [sys.executable, str(SERVER), "--role", "redialrole",
             "--state", str(self.proj / ".pair"), "--socket", self.sock],
            cwd=str(self.proj), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True)
        self.procs.append(proc)
        for _ in range(200):
            if os.path.exists(self.sock):
                try:
                    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    probe.settimeout(0.3)
                    probe.connect(self.sock)
                    probe.close()
                    return proc
                except OSError:
                    pass
            self.assertIsNone(proc.poll(), "daemon exited early")
            time.sleep(0.05)
        self.fail("daemon never became connectable")
        return proc

    def _kill_daemon(self) -> int:
        """Kill the process holding this role's lock — the fd-scan, the only
        liveness proof (round 108's lesson: argv/pgrep checks returned empty
        for live servers). Returns the dead pid, 0 if none held the role."""
        lock = str((self.proj / ".pair" / f".server-redialrole.lock").resolve())
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            fd_dir = Path("/proc") / entry / "fd"
            try:
                fds = os.listdir(fd_dir)
            except OSError:
                continue
            for fd in fds:
                try:
                    linked = os.readlink(fd_dir / fd)
                except OSError:
                    continue
                if linked == lock:
                    subprocess.run(["kill", entry], capture_output=True, timeout=10)
                    time.sleep(0.3)
                    return int(entry)
        return 0

    def _client(self, redial: bool) -> subprocess.Popen:
        env = dict(os.environ)
        env["PAIR_SOCKET_REDIAL"] = REDIAL if redial else "0"
        proc = subprocess.Popen(
            [sys.executable, str(CLIENT), "redialrole",
             "--state", str(self.proj / ".pair"), "--socket", self.sock],
            cwd=str(self.proj), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1, env=env)
        self.procs.append(proc)
        return proc

    def _call(self, proc: subprocess.Popen, rid: int, timeout: float = 8) -> dict:
        proc.stdin.write(json.dumps(
            {"jsonrpc": "2.0", "id": rid, "method": "tools/call",
             "params": {"name": "pair_state", "arguments": {}}}) + "\n")
        proc.stdin.flush()
        line = proc.stdout.readline()
        return json.loads(line) if line else {}

    def test_client_serves_after_the_daemon_dies_and_a_new_one_arrives(self) -> None:
        """THE acceptance: the daemon dies under the client, a NEW daemon is
        raised, and the client's NEXT frame is served — no CLI restart."""
        self._daemon()
        client = self._client(redial=True)
        self.assertIn("result", self._call(client, 1))
        # the daemon dies beneath the client: identify it by the lock it holds,
        # never by argv shape (round 108's lesson — pgrep/argv checks lied)
        dead = self._kill_daemon()
        self.assertTrue(dead, "the test must actually kill the daemon")
        time.sleep(0.5)
        # a new daemon is raised, as pairup would
        self._daemon()
        reply = self._call(client, 2)
        self.assertIn("result", reply,
                      f"the client must re-dial and be served by the NEW daemon: {reply}")

    def test_without_redial_the_client_still_reports_death(self) -> None:
        """The default lane is unchanged: redial=0 keeps today's behaviour — the
        client reports the dead daemon rather than silently hanging."""
        self._daemon()
        client = self._client(redial=False)
        self.assertIn("result", self._call(client, 1))
        dead = self._kill_daemon()
        self.assertTrue(dead, "the test must actually kill the daemon")
        time.sleep(0.5)
        # The client is blocked in stdin.readline(): a CLI that simply closes its
        # stdin ends the session, and "daemon died" would be moot (exit 0, quietly
        # — measured). The honest probe is to SEND a frame: then the write
        # succeeds and the reply never comes, which is the reportable case.
        try:
            client.stdin.write('{"jsonrpc":"2.0","id":2,"method":"ping"}\n')
            client.stdin.flush()
        except OSError:
            pass
        # The stdin is closed by communicate() itself. Closing it by hand first
        # makes Python 3.8+ flush a closed stream inside communicate() and raise
        # "I/O operation on closed file" - a race that only bites when the
        # client is still alive (measured in CI: 5 of 5 Python versions).
        try:
            out, err = client.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            client.kill()
            out, err = client.communicate()
        combined = (out or "") + (err or "")
        self.assertIn("daemon", combined.lower(),
                      f"the dead daemon must be reported, not hidden: {combined[:200]!r}")

    def test_redial_is_bounded(self) -> None:
        """Bounded, not a supervisor: with no daemon ever coming back, the client
        gives up inside its window and reports the failure — it never hangs
        forever waiting for a daemon that is not coming."""
        start = time.time()
        env = dict(os.environ)
        env["PAIR_SOCKET_REDIAL"] = "3"
        proc = subprocess.run(
            [sys.executable, str(CLIENT), "redialrole",
             "--state", str(self.proj / ".pair"), "--socket", self.sock],
            cwd=str(self.proj), capture_output=True, text=True,
            timeout=60, env=env,
            input='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n')
        elapsed = time.time() - start
        self.assertLess(elapsed, 30, f"the client must not hang: {elapsed:.1f}s")
        self.assertNotEqual(proc.returncode, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
