#!/usr/bin/env python3
"""Tests for the thin socket client (round 106 slice F).

The human-visible fact this slice delivers: a CLI restart RECONNECTS instead of
restarting the server, and a daemon death is recovered by the supervisor, not by
the CLI. Both are tested with real processes — a mocked pipe proves nothing about
lifetimes.

Run: python3 mcp/pair_mailbox/test_socket_client.py
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


class ThinClientCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.proj = Path(self._tmp.name) / "proj"
        (self.proj / ".pair").mkdir(parents=True, exist_ok=True)
        (self.proj / ".pair" / "state.json").write_text(json.dumps(
            {"round": 901, "mode": "fix", "writer": "omp", "roles": {},
             "phase": "implement", "seq": 0}), encoding="utf-8")
        self.sock_path = str(self.proj / ".pair" / ".server-testcli.sock")
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
            [sys.executable, str(SERVER), "--role", "testcli",
             "--state", str(self.proj / ".pair"), "--socket", self.sock_path],
            cwd=str(self.proj), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True)
        self.procs.append(proc)
        for _ in range(200):
            if os.path.exists(self.sock_path):
                try:
                    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    probe.settimeout(0.3)
                    probe.connect(self.sock_path)
                    probe.close()
                    return proc
                except OSError:
                    pass
            self.assertIsNone(proc.poll(), "daemon exited before serving")
            time.sleep(0.05)
        self.fail("daemon never became connectable")

    def _client(self) -> subprocess.Popen:
        """A CLI-shaped child: stdin we write to, stdout we read from."""
        proc = subprocess.Popen(
            [sys.executable, str(CLIENT), "testcli",
             "--state", str(self.proj / ".pair"), "--socket", self.sock_path],
            cwd=str(self.proj), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1)
        self.procs.append(proc)
        return proc

    def _roundtrip(self, proc: subprocess.Popen, rid: int) -> dict:
        proc.stdin.write(json.dumps(
            {"jsonrpc": "2.0", "id": rid, "method": "tools/call",
             "params": {"name": "pair_state", "arguments": {}}}) + "\n")
        proc.stdin.flush()
        line = proc.stdout.readline()
        return json.loads(line) if line else {}

    def test_client_connects_and_round_trips(self) -> None:
        self._daemon()
        client = self._client()
        reply = self._roundtrip(client, 1)
        self.assertIn("result", reply)
        self.assertEqual(reply.get("id"), 1)

    def test_cli_restart_reconnects_without_restarting_the_daemon(self) -> None:
        """THE human-visible fact: a CLI restart reconnects instead of
        restarting the server. The daemon's pid is unchanged and the second
        CLI gets a working transport — no relaunch, no lost session."""
        daemon = self._daemon()
        first = self._client()
        self.assertIn("result", self._roundtrip(first, 1))
        # the CLI goes away the way a restart does
        first.stdin.close()
        try:
            first.wait(timeout=15)
        except subprocess.TimeoutExpired:
            first.kill()
        self.assertIsNone(daemon.poll(),
                          "the daemon must survive its CLI's departure")
        second = self._client()
        reply = self._roundtrip(second, 2)
        self.assertIn("result", reply,
                      "a reconnecting CLI must be served without a server restart")
        self.assertEqual(reply.get("id"), 2)
        self.assertIsNone(daemon.poll(), "still one daemon, not a second")

    def test_missing_daemon_refuses_with_a_named_remedy(self) -> None:
        """A client that SILENTLY spawned a server would rebuild the child-lifetime
        defect this slice removes. It refuses and names what raises the daemon."""
        result = subprocess.run(
            [sys.executable, str(CLIENT), "testcli",
             "--state", str(self.proj / ".pair"), "--socket", self.sock_path],
            cwd=str(self.proj), capture_output=True, text=True, timeout=60)
        self.assertNotEqual(result.returncode, 0)
        combined = (result.stderr or "") + (result.stdout or "")
        self.assertIn("no daemon", combined)
        self.assertIn("pairup", combined)

    def test_daemon_death_is_recovered_by_a_new_daemon_not_the_client(self) -> None:
        """The other half of the ruling: a daemon death is recovered by the
        supervisor raising a daemon, and a fresh client then works. The client
        never spawns, so the recovery is a DAEMON, not a new child of the CLI."""
        first_daemon = self._daemon()
        first_daemon.kill()
        first_daemon.wait(timeout=10)
        time.sleep(0.3)
        # the CLI now finds no daemon: it must refuse, not spawn
        gone = self._client()
        try:
            out, err = gone.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            gone.kill()
            out, err = gone.communicate()
        self.assertIn("no daemon", (err or "") + (out or ""))
        # the supervisor's recovery: a new daemon, and a client connects to it
        second_daemon = self._daemon()
        client = self._client()
        reply = self._roundtrip(client, 3)
        self.assertIn("result", reply)


if __name__ == "__main__":
    unittest.main(verbosity=2)
