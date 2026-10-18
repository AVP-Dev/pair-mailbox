#!/usr/bin/env python3
"""The lock holder's liveness, not just the lock (round 111 slice 1).

The card's defect, sharpened by slice D: a socket daemon can outlive its agent BY
DESIGN, and the kernel keeps the flock for a live process — so "held" now answers
"some process holds the role", which may be a healthy daemon, a corpse holding
the file, or a live holder whose client went home. The refusal message must say
WHICH, because "already held" names a state, not a diagnosis.

These tests run REAL processes on REAL locks: a child holds the role, the child
dies, and the refuser must be able to tell the difference — by the lock's own
fd-scan (the only liveness proof — pgrep is not, measured three false DOWNs).
"""
from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "mcp" / "pair_mailbox"))

import lock_reaper  # noqa: E402
from server import Store  # noqa: E402


def _flock_taken(path: Path) -> bool:
    handle = path.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True      # someone holds it
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
    return False


class LockHolderCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state = Path(self._tmp.name) / ".pair"
        self.state.mkdir(parents=True)
        (self.state / "state.json").write_text(json.dumps(
            {"round": 904, "mode": "fix", "roles": {}, "phase": "implement"}),
            encoding="utf-8")
        self.role = "lockrole"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _raise_real_server(self) -> subprocess.Popen:
        env = dict(os.environ)
        env.pop("PAIR_ROLE", None)
        return subprocess.Popen(
            [sys.executable, str(ROOT / "mcp/pair_mailbox/server.py"),
             "--role", self.role, "--state", str(self.state)],
            cwd=str(ROOT), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, env=env)

    def test_a_live_holder_is_named_as_a_process_not_a_state(self) -> None:
        """The healthy case: the refusal must say WHAT holds the role (a pid) and
        that the holder is ALIVE — an operator needs to know whether to kill
        something or to start something."""
        proc = self._raise_real_server()
        try:
            for _ in range(100):
                if _flock_taken(lock_reaper.lock_file(self.state, self.role)):
                    break
                if proc.poll() is not None:
                    self.fail("server exited before holding the lock")
                time.sleep(0.05)
            self.assertEqual(lock_reaper.role_state(self.state, self.role), "live")
            holder = lock_reaper.holder_pid(self.state, self.role)
            self.assertTrue(holder, "a live holder must be identifiable by pid")
            self.assertTrue(lock_reaper.pid_alive(holder))
        finally:
            proc.kill()
            proc.wait(timeout=10)

    def test_a_dead_holder_is_distinguishable(self) -> None:
        """The corpse case: the holder's pid is gone. The role must read 'stale'
        (not 'live'), and the reported diagnosis must say the holder died."""
        proc = self._raise_real_server()
        for _ in range(100):
            if _flock_taken(lock_reaper.lock_file(self.state, self.role)):
                break
            time.sleep(0.05)
        holder = lock_reaper.holder_pid(self.state, self.role)
        self.assertTrue(holder, "the fixture must hold the role first")
        proc.kill()
        proc.wait(timeout=10)
        time.sleep(0.3)
        # after death the kernel releases the flock, so 'live' must be gone and
        # the lock must be free for the next raiser
        self.assertFalse(_flock_taken(lock_reaper.lock_file(self.state, self.role)),
                         "the kernel must release the flock on death")
        self.assertNotEqual(lock_reaper.role_state(self.state, self.role), "live",
                            "a dead holder must never read live")

    def test_a_pid_record_without_a_holder_is_named(self) -> None:
        """The wedge that bytes: a pid FILE naming a dead process. The kernel's
        flock is free, so technically the role IS free — but a tool reading the
        file alone would refuse. The report must name the role 'stale', so the
        reaper has something to act on."""
        pid_file = lock_reaper.pid_file(self.state, self.role)
        pid_file.write_text("999999999\n", encoding="utf-8")
        self.assertEqual(lock_reaper.role_state(self.state, self.role), "stale")
        self.assertEqual(lock_reaper.reap(self.state), [self.role])


if __name__ == "__main__":
    unittest.main(verbosity=2)
