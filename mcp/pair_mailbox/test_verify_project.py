"""Tests for scripts/verify-project.sh: one gate per worktree.

Round 137. Every gate run writes the same artifacts - `.pair/verify-<name>.log`
for each check and `.pair/verify-results.txt` for the totals - and a run
REMOVES both at its start. Two runners in one worktree therefore do not
merely race: the second one deletes the first one's results, and each run's
totals count the other's rows. The room measured it: 83 result rows where 78
checks had run, and a "0 required failures" that belonged to neither run.

The fix is a worktree lock (scripts/verify-lock.sh) and a loud refusal. These
tests drive the REAL gate script in a temporary project, because a test that
re-implements the locking cannot see the locking break - the same rule the
routing tests follow for verify-changed.sh.

Run: python3 mcp/pair_mailbox/test_verify_project.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = ROOT / "scripts"


class TempProject:
    """A throwaway project with the gate script and a profile of our own."""

    def __init__(self, command: str = "true") -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "scripts").mkdir()
        for name in ("verify-project.sh", "verify-lock.sh"):
            shutil.copy(SCRIPTS / name, self.root / "scripts" / name)
        self.profile = self.root / "scripts" / "profile.tsv"
        self.profile.write_text(
            f"slow\tpositive\t1\t{command}\n", encoding="utf-8")

    @property
    def gate(self) -> Path:
        return self.root / "scripts" / "verify-project.sh"

    @property
    def lock_dir(self) -> Path:
        return self.root / ".pair" / "verify.lock.d"

    def run_gate(self, timeout: int = 60) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(self.gate), "--profile", "scripts/profile.tsv",
             "--only", "positive"],
            cwd=self.root, capture_output=True, text=True, timeout=timeout)

    def cleanup(self) -> None:
        self.tmp.cleanup()


class TestWorktreeLock(unittest.TestCase):
    def test_a_second_gate_in_the_same_worktree_is_refused(self) -> None:
        """The loser must be told, not left to delete the winner's evidence."""
        project = TempProject(command="sleep 2")
        self.addCleanup(project.cleanup)
        winner = subprocess.Popen(
            ["bash", str(project.gate), "--profile", "scripts/profile.tsv",
             "--only", "positive"],
            cwd=project.root, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True)
        try:
            # Wait for the winner to hold the lock.
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not project.lock_dir.exists():
                time.sleep(0.05)
            self.assertTrue(project.lock_dir.exists(),
                            "the first gate never took the lock")
            loser = project.run_gate()
            self.assertEqual(75, loser.returncode,
                             f"the second gate was not refused: {loser.stderr}")
            self.assertIn("another gate is running", loser.stderr)
        finally:
            out, err = winner.communicate(timeout=60)
        self.assertEqual(0, winner.returncode,
                         f"the first gate failed: {err or out}")

    def test_the_refused_gate_keeps_the_artifacts_of_the_winner(self) -> None:
        """A refusal must not touch what the running gate is writing."""
        project = TempProject(command="sleep 2")
        self.addCleanup(project.cleanup)
        winner = subprocess.Popen(
            ["bash", str(project.gate), "--profile", "scripts/profile.tsv",
             "--only", "positive"],
            cwd=project.root, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True)
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not project.lock_dir.exists():
                time.sleep(0.05)
            log = project.root / ".pair" / "verify-slow.log"
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not log.exists():
                time.sleep(0.05)
            before = log.read_text(encoding="utf-8") if log.exists() else ""
            project.run_gate()
            after = log.read_text(encoding="utf-8") if log.exists() else ""
            self.assertEqual(before, after,
                             "the refused gate rewrote the winner's log")
        finally:
            winner.communicate(timeout=60)

    def test_a_stale_lock_is_taken_over(self) -> None:
        """A crashed run must not block the room forever."""
        project = TempProject()
        self.addCleanup(project.cleanup)
        project.lock_dir.mkdir(parents=True)
        (project.lock_dir / "pid").write_text("999999\n", encoding="utf-8")
        proc = project.run_gate()
        self.assertEqual(0, proc.returncode,
                         f"a stale lock blocked the gate: {proc.stderr}")

    def test_a_normal_run_leaves_no_lock_behind(self) -> None:
        """A lock that outlives its run is a lock that blocks the next run."""
        project = TempProject()
        self.addCleanup(project.cleanup)
        proc = project.run_gate()
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertFalse(project.lock_dir.exists(),
                         "the gate left its lock behind")

    def test_the_helper_is_reentrant_within_one_process_tree(self) -> None:
        """The nested gate (verify-changed.sh -> verify-project.sh) must not
        deadlock against its own parent: the second take is a no-op."""
        project = TempProject()
        self.addCleanup(project.cleanup)
        script = (
            '. "$1/scripts/verify-lock.sh"; '
            'acquire_worktree_lock "$1" || exit 3; '
            'acquire_worktree_lock "$1" || exit 4; '
            'exit 0')
        proc = subprocess.run(["bash", "-c", script, "bash", str(project.root)],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(0, proc.returncode,
                         f"the helper refused its own second take: "
                         f"{proc.stdout}{proc.stderr}")

    def test_a_leaked_token_does_not_open_a_lock_it_never_took(self) -> None:
        """A boolean in the environment leaks into every descendant.

        Round 137, found by the changed-file gate: the first version of the
        lock exported a boolean, so every process a suite spawned inherited it
        and believed it held a lock it never took. The token matches only the
        lock it was issued for, so a leaked value is ignored - and the process
        still takes the lock.
        """
        project = TempProject()
        self.addCleanup(project.cleanup)
        env = {**os.environ, "VERIFY_WORKTREE_LOCK": "999999-some-other-lock"}
        proc = subprocess.run(
            ["bash", "-c",
             '. "$1/scripts/verify-lock.sh"; acquire_worktree_lock "$1" || exit 3; '
             'exit 0',
             "bash", str(project.root)],
            capture_output=True, text=True, timeout=60, env=env)
        self.assertEqual(0, proc.returncode,
                         f"a leaked token broke the lock: {proc.stdout}{proc.stderr}")
        self.assertTrue(project.lock_dir.exists(),
                        "the process with the leaked token took no lock")

    def test_the_helper_refuses_a_second_process(self) -> None:
        """Two processes, one worktree: the second is refused."""
        project = TempProject()
        self.addCleanup(project.cleanup)
        holder = subprocess.Popen(
            ["bash", "-c",
             '. "$1/scripts/verify-lock.sh"; acquire_worktree_lock "$1" || exit 3; '
             'sleep 2',
             "bash", str(project.root)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not project.lock_dir.exists():
                time.sleep(0.05)
            self.assertTrue(project.lock_dir.exists(),
                            "the holder never took the lock")
            other = subprocess.run(
                ["bash", "-c",
                 '. "$1/scripts/verify-lock.sh"; acquire_worktree_lock "$1"; '
                 'exit $?',
                 "bash", str(project.root)],
                capture_output=True, text=True, timeout=60)
            self.assertEqual(1, other.returncode,
                             f"a second process took the lock: {other.stdout}")
        finally:
            holder.communicate(timeout=60)


if __name__ == "__main__":
    unittest.main(verbosity=2)
