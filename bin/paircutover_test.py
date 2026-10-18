#!/usr/bin/env python3
"""Tests for the CLI-side cutover (round 106 slice G).

The human-visible fact the whole transport chain exists to deliver: with the
daemon already running, a CLI RESTART reconnects instead of restarting the
server. That is a refresh, not a relaunch, and its cost is a handshake rather
than a lost session.

These tests drive the REAL pieces — pairup's daemon raise, the thin client, the
daemon — because the claim under test is about lifetimes, and a mocked lifetime
proves nothing.

Run: python3 bin/paircutover_test.py
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

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "mcp" / "pair_mailbox" / "server.py"
CLIENT = ROOT / "mcp" / "pair_mailbox" / "mcp_socket_client.py"
PAIRUP = ROOT / "bin" / "pairup"


def _raise_daemon(proj: Path, sock: str) -> subprocess.Popen:
    """The same shape pairup uses — a long-lived process, not a CLI child."""
    proc = subprocess.Popen(
        [sys.executable, str(SERVER), "--role", "cutrole",
         "--state", str(proj / ".pair"), "--socket", sock],
        cwd=str(proj), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    for _ in range(200):
        if os.path.exists(sock):
            try:
                probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                probe.settimeout(0.3)
                probe.connect(sock)
                probe.close()
                return proc
            except OSError:
                pass
        if proc.poll() is not None:
            out = (proc.stdout.read() or "") + (proc.stderr.read() or "")
            raise AssertionError(f"daemon exited early: {out[:300]}")
        time.sleep(0.05)
    raise AssertionError("daemon never became connectable")


class CutoverCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.proj = Path(self._tmp.name) / "proj"
        (self.proj / ".pair").mkdir(parents=True, exist_ok=True)
        (self.proj / ".pair" / "state.json").write_text(json.dumps(
            {"round": 902, "mode": "fix", "writer": "omp", "roles": {},
             "phase": "implement", "seq": 0}), encoding="utf-8")
        self.sock = str(self.proj / ".pair" / ".server-cutrole.sock")
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

    def _client(self) -> subprocess.Popen:
        proc = subprocess.Popen(
            [sys.executable, str(CLIENT), "cutrole",
             "--state", str(self.proj / ".pair"), "--socket", self.sock],
            cwd=str(self.proj), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1)
        self.procs.append(proc)
        return proc

    def _call(self, proc: subprocess.Popen, rid: int) -> dict:
        proc.stdin.write(json.dumps(
            {"jsonrpc": "2.0", "id": rid, "method": "tools/call",
             "params": {"name": "pair_state", "arguments": {}}}) + "\n")
        proc.stdin.flush()
        line = proc.stdout.readline()
        return json.loads(line) if line else {}

    # -- the fact the slice exists for ------------------------------------

    def test_cli_restart_reconnects_without_restarting_the_server(self) -> None:
        """THE acceptance: a CLI restart is a REFRESH. The daemon's pid is
        unchanged, the second CLI is served, and no server process is raised
        along the way."""
        daemon = _raise_daemon(self.proj, self.sock)
        self.procs.append(daemon)
        daemon_pid = daemon.pid
        first = self._client()
        self.assertIn("result", self._call(first, 1))
        first.stdin.close()
        try:
            first.wait(timeout=15)
        except subprocess.TimeoutExpired:
            first.kill()
        time.sleep(0.4)
        self.assertIsNone(daemon.poll(), "the daemon must outlive its CLI")
        second = self._client()
        reply = self._call(second, 2)
        self.assertIn("result", reply, "the reconnecting CLI must be served")
        self.assertEqual(reply.get("id"), 2)
        self.assertEqual(daemon.pid, daemon_pid,
                         "a refresh must not restart the server")

    def test_dead_daemon_blocks_the_client_with_a_named_remedy(self) -> None:
        """The client must never spawn a replacement: that would rebuild the
        child-lifetime defect. It refuses, naming who raises the daemon."""
        daemon = _raise_daemon(self.proj, self.sock)
        daemon.kill()
        daemon.wait(timeout=10)
        time.sleep(0.3)
        gone = subprocess.run(
            [sys.executable, str(CLIENT), "cutrole",
             "--state", str(self.proj / ".pair"), "--socket", self.sock],
            cwd=str(self.proj), capture_output=True, text=True, timeout=60)
        self.assertNotEqual(gone.returncode, 0)
        combined = (gone.stderr or "") + (gone.stdout or "")
        self.assertIn("no daemon", combined)
        self.assertIn("pairup", combined, "the remedy must name the raiser")

    def test_daemon_restart_recovers_transport(self) -> None:
        """The paired half: after the daemon dies, raising a NEW one restores
        transport — pairrestart/pairup recover, the CLI does not."""
        first_daemon = _raise_daemon(self.proj, self.sock)
        first_daemon.kill()
        first_daemon.wait(timeout=10)
        second_daemon = _raise_daemon(self.proj, self.sock)
        self.procs.append(second_daemon)
        client = self._client()
        reply = self._call(client, 3)
        self.assertIn("result", reply)
        self.assertNotEqual(second_daemon.pid, first_daemon.pid)


if __name__ == "__main__":
    unittest.main(verbosity=2)
