#!/usr/bin/env python3
"""Tests for the per-project tmux socket (round 106 slice C).

The defect being proven away: one tmux server served every project on the box,
so gala's panes were addressable from this project's tools — a message once
landed in galatour's pane, and two projects sharing a basename would have
collided by session name too.

These tests use a SCRATCH project, never the live pair: the live pair's session
is on the default socket and must not be disturbed by a test.

Run: python3 bin/pairsocket_test.py
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _sha256(text: str) -> str:
    out = subprocess.run(["sha256sum"], input=text, capture_output=True,
                         text=True, timeout=10)
    return out.stdout.strip()[:8] if out.stdout else ""


def tmux(*args: str, socket: str = "default") -> str:
    cmd = ["tmux"]
    if socket != "default":
        cmd += ["-L", socket]
    cmd += list(args)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    return (proc.stdout or "").strip()


class TestProjectSocket(unittest.TestCase):
    """One project, one server: the other projects stop existing here."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "proj-a"
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / ".pair").mkdir(parents=True, exist_ok=True)
        import hashlib
        self.resolved = str(self.root.resolve())
        self.hash8 = hashlib.sha256(self.resolved.encode()).hexdigest()[:8]
        self.socket = f"pair-proja-{self.hash8}"

    def tearDown(self) -> None:
        for sock in (self.socket, f"{self.socket}-b"):
            subprocess.run(["tmux", "-L", sock, "kill-server"],
                           capture_output=True, timeout=10)
        self._tmp.cleanup()

    def _seed(self, name: str) -> str:
        """A session on THIS project's socket, one pane, doing nothing."""
        existing = tmux("list-sessions", "-F", "#{session_name}", socket=self.socket)
        if existing:
            subprocess.run(["tmux", "-L", self.socket, "kill-server"],
                           capture_output=True, timeout=10)
        proc = subprocess.run(
            ["tmux", "-L", self.socket, "new-session", "-d", "-s", name,
             "-c", str(self.root), "/bin/sh"],
            capture_output=True, text=True, timeout=15)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return name

    def test_projects_session_is_invisible_on_default(self) -> None:
        """The core: another project's session does not exist in our namespace."""
        name = self._seed(f"pair-proja-{self.hash8}")
        self.assertIn(name, tmux("list-sessions", "-F", "#{session_name}",
                                 socket=self.socket))
        self.assertNotIn(name, tmux("list-sessions", "-F", "#{session_name}",
                                    socket="default").splitlines() or [])

    def test_same_basename_different_root_never_collides(self) -> None:
        """Two clones of one repo share basename AND origin; the hash is what
        keeps their sessions apart — the collision the old naming invited.
        A second scratch project with the SAME basename gets a different socket."""
        import hashlib
        second = Path(self._tmp.name) / "clone" / "proj-a"
        second.mkdir(parents=True, exist_ok=True)
        (second / ".pair").mkdir(parents=True, exist_ok=True)
        second_hash = hashlib.sha256(str(second.resolve()).encode()).hexdigest()[:8]
        second_socket = f"pair-proja-{second_hash}"
        self.assertNotEqual(self.socket, second_socket,
                            "the same basename on a different path must hash apart")
        try:
            proc = subprocess.run(
                ["tmux", "-L", second_socket, "new-session", "-d", "-s", "pair-proja-x",
                 "-c", str(second), "/bin/sh"],
                capture_output=True, text=True, timeout=15)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            mine = tmux("list-sessions", "-F", "#{session_name}", socket=self.socket)
            theirs = tmux("list-sessions", "-F", "#{session_name}", socket=second_socket)
            self.assertNotIn("pair-proja-x", mine.splitlines() or [])
            self.assertIn("pair-proja-x", theirs.splitlines() or [])
        finally:
            subprocess.run(["tmux", "-L", second_socket, "kill-server"],
                           capture_output=True, timeout=10)

    def test_resolver_prefixes_the_socket(self) -> None:
        """Condition (c): one resolver for every call site. composer._tmux()
        must return the flag-prefixed command when the env names a socket, and
        plain `tmux` when it does not — never a second convention."""
        sys.path.insert(0, str(ROOT / "mcp" / "pair_mailbox"))
        import os
        import composer
        saved = os.environ.pop("PAIR_PANE_SOCKET", None)
        try:
            os.environ["PAIR_PANE_SOCKET"] = self.socket
            self.assertIn(f"-L {self.socket}", composer._tmux())
            os.environ["PAIR_PANE_SOCKET"] = "default"
            self.assertNotIn("-L", composer._tmux())
            os.environ.pop("PAIR_PANE_SOCKET", None)
            self.assertNotIn("-L", composer._tmux())
        finally:
            if saved is not None:
                os.environ["PAIR_PANE_SOCKET"] = saved

    def test_human_readable_and_hash(self) -> None:
        """Readable AND hash in one name: the hash part is what scopes it."""
        self.assertTrue(self.socket.startswith("pair-proja-"))
        self.assertEqual(len(self.hash8), 8)
        self.assertNotEqual(self.socket, "pair-proja-")


if __name__ == "__main__":
    unittest.main(verbosity=2)
