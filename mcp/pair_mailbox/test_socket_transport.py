#!/usr/bin/env python3
"""Tests for the unix-socket transport (round 106 slice D).

The acceptance the critic set: the transport must OUTLIVE the CLI — a CLI dying
does not take the server with it. These tests run the server as a REAL process
on a real socket, kill the client, reconnect, and assert the transport is still
working. Nothing mocked: a mocked socket proves nothing about staying alive.

Run: python3 mcp/pair_mailbox/test_socket_transport.py
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


class Client:
    """One connection speaking the same newline-delimited framing."""

    def __init__(self, path: str, timeout: float = 8) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect(path)
        self.reader = self.sock.makefile("r", encoding="utf-8", newline="\n")
        self.writer = self.sock.makefile("w", encoding="utf-8", newline="\n")

    def call(self, method: str, params: dict, rid: int = 1) -> dict:
        """Raw JSON-RPC, e.g. `initialize` or `tools/list`."""
        self.writer.write(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method,
                                      "params": params}) + "\n")
        self.writer.flush()
        line = self.reader.readline()
        return json.loads(line) if line else {}

    def tool(self, name: str, arguments: dict | None = None, rid: int = 1) -> dict:
        """The MCP tool convention: tools/call with name + arguments."""
        return self.call("tools/call", {"name": name, "arguments": arguments or {}}, rid)

    def close(self) -> None:
        try:
            self.reader.close()
            self.writer.close()
            self.sock.close()
        except OSError:
            pass

    def kill_hard(self) -> None:
        """Close without a goodbye, dropping every handle: what a dying CLI does.
        Closing only the socket while the makefile buffers stay open lets a
        later readline() block forever on a reader nobody will ever fill."""
        for handle in (getattr(self, "reader", None), getattr(self, "writer", None)):
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    pass
        try:
            self.sock.close()
        except OSError:
            pass


class SocketTransportCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.proj = self.tmp / "proj"
        (self.proj / ".pair").mkdir(parents=True, exist_ok=True)
        (self.proj / ".pair" / "state.json").write_text(json.dumps(
            {"round": 900, "mode": "fix", "writer": "omp", "roles": {},
             "phase": "implement", "seq": 0}), encoding="utf-8")
        self.sock_path = str(self.proj / ".pair" / ".server-testrole.sock")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _spawn(self, once: bool = False) -> subprocess.Popen:
        cmd = [sys.executable, str(SERVER), "--role", "testrole",
               "--state", str(self.proj / ".pair"), "--socket"]
        cmd += [self.sock_path]
        proc = subprocess.Popen(
            cmd, cwd=str(self.proj), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env={**os.environ, "PAIR_SOCKET_ONCE": "1" if once else "0"})
        # Wait for the socket to exist, and PROVE it is acceptable only for a
        # daemon (`once=False`): a probe connect on a ONE-SHOT server would
        # consume its single client and the real test would hang. For `once=True`
        # the bind log is the signal — the server prints it before accepting.
        for _ in range(200):
            if proc.poll() is not None:
                out = (proc.stdout.read() or "") + (proc.stderr.read() or "")
                self.fail(f"server exited before serving: {out[:400]}")
            if os.path.exists(self.sock_path):
                if once:
                    return proc
                try:
                    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    probe.settimeout(0.3)
                    probe.connect(self.sock_path)
                    probe.close()
                    return proc
                except OSError:
                    pass
            time.sleep(0.05)
        self.fail("socket never became connectable")

    def test_round_trip_over_the_socket(self) -> None:
        """The same protocol answers over the socket as it does over stdio."""
        proc = self._spawn(once=True)
        client = Client(self.sock_path)
        reply = client.tool("pair_state", {}, rid=1)
        client.close()
        proc.wait(timeout=10)
        self.assertIn("result", reply, reply)
        self.assertEqual(reply.get("id"), 1)

    def test_server_outlives_a_dying_client(self) -> None:
        """THE acceptance: a CLI dying does not take the transport with it.
        First client is killed hard; a NEW client connects and gets a working
        transport. That is the refresh shape the human asked for."""
        proc = self._spawn(once=False)
        try:
            first = Client(self.sock_path)
            reply = first.tool("pair_state", {}, rid=7)
            self.assertIn("result", reply)
            first.kill_hard()
            time.sleep(0.4)
            self.assertIsNone(proc.poll(),
                              "the server must survive its client's death")
            second = Client(self.sock_path)
            reply2 = second.tool("pair_state", {}, rid=8)
            second.close()
            self.assertIn("result", reply2, "a reconnecting client must be served")
            self.assertEqual(reply2.get("id"), 8)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()

    def test_role_lock_is_held_while_the_socket_server_lives(self) -> None:
        """A second server on the same role must REFUSE: the lock is per role
        per state dir, and the socket server holds it for its whole life —
        otherwise two daemons would collide on one role."""
        proc = self._spawn(once=False)
        try:
            # Popen, then communicate(timeout): a REFUSING server exits at once,
            # while a wrongly-accepted one would hang as a daemon — and a hang IS
            # the failure being tested for.
            second = subprocess.Popen(
                [sys.executable, str(SERVER), "--role", "testrole",
                 "--state", str(self.proj / ".pair"), "--socket", self.sock_path],
                cwd=str(self.proj), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True)
            refused = False
            try:
                out, err = second.communicate(timeout=20)
                combined = (out or "") + (err or "")
                self.assertNotEqual(second.returncode, 0,
                                    "a second server on a held role must refuse")
                # Round 111 slice 1 changed the phrasing from a claim
                # ("already held by a live server") to a DIAGNOSIS ("held by a
                # LIVE process (pid N)"); this assertion followed the words rather
                # than the contract. Contract: refusal + named holder + remedy.
                self.assertIn("held by a LIVE process", combined)
                self.assertRegex(combined, r"pid \d+")
                refused = True
            except subprocess.TimeoutExpired:
                second.kill()
                second.communicate()
            self.assertTrue(refused,
                            "the second server must REFUSE, not run as a daemon")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()

    def test_lock_releases_when_the_server_dies(self) -> None:
        """The kernel releases the flock on death, so no dead daemon wedges the
        role — the named risk of a daemon outliving its agent. The proof: the
        dying server's lock is gone, so the NEXT server binds instead of
        refusing."""
        dead = self._spawn(once=False)
        dead.kill()
        dead.wait(timeout=10)
        time.sleep(0.3)
        # a fresh daemon must bind — if the stale lock stuck, main() would refuse
        fresh = self._spawn(once=False)
        self.assertIsNone(fresh.poll(), "the lock must be released on death")
        client = Client(self.sock_path)
        reply = client.tool("pair_state", {}, rid=3)
        client.close()
        self.assertIn("result", reply)
        fresh.terminate()
        try:
            fresh.wait(timeout=10)
        except subprocess.TimeoutExpired:
            fresh.kill()


if __name__ == "__main__":
    unittest.main(verbosity=2)
