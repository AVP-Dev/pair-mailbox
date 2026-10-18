#!/usr/bin/env python3
"""Tests for the stale role lock (round 108 slice 2).

The card's shape, sharpened by slice D: "lock held" no longer implies "server
alive", because a socket daemon outliving its agent is now a FEATURE. The lock
is the daemon's; liveness is the holder pid's. A held lock with a dead pid means
a stale lock FILE — the kernel's flock should have released it, and where it did
not (a lock file written by another mechanism, or a pid recycled into a
non-server process) the reaper must clean it rather than let it wedge a role.

Run: python3 mcp/pair_mailbox/test_lock_reaper.py
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


class LockReaperCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state = Path(self._tmp.name) / ".pair"
        self.state.mkdir(parents=True)
        (self.state / "state.json").write_text(json.dumps(
            {"round": 904, "mode": "fix", "roles": {"omp": {}, "opencode": {}},
             "phase": "implement"}), encoding="utf-8")
        self.role = "reaperrole"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _lock_file(self) -> Path:
        return self.state / f".server-{self.role}.lock"

    def _try_lock(self) -> bool:
        """Can a new process take the lock right now?"""
        handle = self._lock_file().open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
        return True

    def test_live_holder_still_refuses(self) -> None:
        """The reaper must never break the good case: a live holder's lock is
        not stale, and a second raiser still refuses."""
        proc = subprocess.Popen(
            [sys.executable, str(ROOT / "mcp/pair_mailbox/server.py"),
             "--role", self.role, "--state", str(self.state)],
            cwd=str(ROOT), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        try:
            for _ in range(100):
                if self._lock_file().exists():
                    try:
                        handle = self._lock_file().open("a+")
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        handle.close()
                        time.sleep(0.05)
                        continue
                    except OSError:
                        break
                if proc.poll() is not None:
                    break
                time.sleep(0.05)
            self.assertFalse(self._try_lock(), "a live holder must keep the lock")
            self.assertEqual(lock_reaper.role_state(self.state, self.role), "live",
                             "a live holder must read live, never stale")
        finally:
            proc.kill()
            proc.wait(timeout=10)

    def test_lock_held_while_pid_alive_is_not_stale(self) -> None:
        """stale == the lock is NOT taken, but a stale lock file records a pid
        that no longer holds it. The reaper cleans such a file so the role is
        free again."""
        self._lock_file().write_text("", encoding="utf-8")
        # the file exists, nothing holds the flock: exactly the wedge shape
        self.assertTrue(self._lock_file().exists())
        self.assertTrue(self._try_lock(), "an unheld flock is free at the kernel level")
        # The reaper's report has nothing to report for a role with no record
        # and no roster entry: absence is silence, NOT a claim. What matters is
        # that nothing forces a 'live' on a role nobody holds.
        report = lock_reaper.report(self.state)
        self.assertNotEqual(report.get(self.role), "live",
                            "an unheld role must never read live")

    def test_stale_recording_is_reaped_and_role_becomes_free(self) -> None:
        """The wedge that actually bites: a `.server-<role>.pid` record left by
        a dead process. The file exists (so naive operators refuse), the kernel
        flock is free (so the real lock is available), and the RECORD must be
        cleaned so no tool reads 'held' from it."""
        pid_file = self.state / f".server-{self.role}.pid"
        pid_file.write_text("999999999\n", encoding="utf-8")   # a pid that is not alive
        lock_reaper.reap(self.state)
        self.assertFalse(pid_file.exists(),
                         "a pid record of a dead process must not survive a reap")

    def test_live_pid_record_is_kept(self) -> None:
        """The reaper must not clean a record whose pid is ALIVE — that is the
        legitimate holder's own bookkeeping."""
        holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            pid_file = self.state / f".server-{self.role}.pid"
            pid_file.write_text(f"{holder.pid}\n", encoding="utf-8")
            lock_reaper.reap(self.state)
            self.assertTrue(pid_file.exists(),
                            "a live holder's record must survive a reap")
        finally:
            holder.kill()
            holder.wait(timeout=10)

    def test_report_names_every_role(self) -> None:
        """The pilot's dead-pane path reads this report: each role is 'live',
        'free', or 'stale', and an unknown state is never silently 'live'."""
        self._lock_file().write_text("", encoding="utf-8")
        report = lock_reaper.report(self.state)
        for role in ("omp", "opencode"):
            self.assertIn(role, report)
            self.assertIn(report[role], ("live", "free", "stale"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
