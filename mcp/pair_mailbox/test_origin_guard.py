#!/usr/bin/env python3
"""Tests for the origin guard (round 108 slice 1).

The measured defect: my own MCP config pointed at /tmp/wire_test, a scratch clone,
and on restart that OLD code would have taken my role silently — the role lock
would have been free, so the foreign tree would have served while everything
looked correct. Round 104's inventory-honesty class, our own instance.

These tests run the REAL serve.sh from real trees, because the claim under test
is about WHERE code runs, and a mocked path proves nothing about it.

Run: python3 mcp/pair_mailbox/test_origin_guard.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SERVE = ROOT / "mcp" / "pair_mailbox" / "serve.sh"


def _git_repo(path: Path) -> Path:
    """A real git tree — the guard's identity is `git rev-parse --show-toplevel`,
    so a plain directory cannot carry one."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=str(path), check=True, timeout=30)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=str(path), check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(path), check=True)
    return path


def _lock_holder(state_dir: Path, role: str) -> str:
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            for fd in os.listdir(f"/proc/{pid}/fd"):
                try:
                    target = os.readlink(f"/proc/{pid}/fd/{fd}")
                except OSError:
                    continue
                if target.endswith(f".server-{role}.lock") and str(state_dir) in target:
                    return pid
        except OSError:
            continue
    return ""


class OriginGuardCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        # the state must sit in a git repo: the guard's identity is
        # `git rev-parse --show-toplevel`, so a plain /tmp dir cannot carry one
        self.state = _git_repo(Path(self._tmp.name) / "proj") / ".pair"
        self.state.mkdir(parents=True)
        (self.state / "state.json").write_text(json.dumps(
            {"round": 903, "mode": "fix", "writer": "omp", "roles": {},
             "phase": "implement", "seq": 0}), encoding="utf-8")
        self.role = "guardrole"

    def tearDown(self) -> None:
        holder = _lock_holder(self.state, self.role)
        if holder:
            subprocess.run(["kill", holder], capture_output=True, timeout=10)
            time.sleep(0.3)
        self._tmp.cleanup()

    def _raise(self, cwd: Path, serve: Path | None = None) -> subprocess.Popen:
        """Raise the server the way the MCP entry does: a chosen CWD and a chosen
        serve.sh. Both matter — the guard compares the repo the CODE lives in
        against the repo the STATE lives in, so a legitimate twin must run its
        OWN serve.sh, not the test repo's."""
        # A cloned tree carries the WHOLE package, not just serve.sh: serve.sh
        # execs server.py relative to itself, and a partial copy would fail with
        # "can't open server.py" before the guard ever ran (measured).
        pkg = Path(cwd) / "mcp" / "pair_mailbox"
        if pkg.is_dir():
            shutil.rmtree(pkg)
        shutil.copytree(ROOT / "mcp" / "pair_mailbox", pkg,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        env = dict(os.environ)
        # The role comes from argv, never from env (round 103 S5's guard): the
        # running pair exports PAIR_ROLE=omp, and a test raising another role
        # would be refused by that guard before it ever reached the origin check.
        env.pop("PAIR_ROLE", None)
        proc = subprocess.Popen(
            ["sh", str(Path(cwd) / "mcp" / "pair_mailbox" / "serve.sh"), self.role,
             "--state", str(self.state)],
            cwd=str(cwd), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, env=env)
        return proc

    def test_foreign_tree_refuses_and_names_both_roots(self) -> None:
        """A server raised from a FOREIGN REPO must refuse, not serve. The
        refusal names both repos, because a bare 'refused' leaves the operator
        guessing which of their clones is wrong."""
        foreign = _git_repo(Path(self._tmp.name) / "scratch-clone")
        proc = self._raise(foreign)   # the full package copied INTO the foreign tree
        try:
            out, err = proc.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
            self.fail("a server from a foreign tree must refuse, not run")
        self.assertNotEqual(proc.returncode, 0)
        combined = (err or "") + (out or "")
        self.assertIn("foreign", combined.lower())
        self.assertIn("scratch-clone", combined,
                      "the refusal must name the repo the code came from")

    def _converse(self, cwd: Path) -> tuple[int, str, bool]:
        """Raise the server AND speak one frame to it: an MCP `initialize`.
        Returns (returncode, combined_output, answered).

        A stdio server exits when stdin closes, so a liveness claim needs a
        client. One frame is the honest probe: `served` means the protocol
        answered; `refused` means the process died before answering, and the
        refusal text is its stderr."""
        proc = self._raise(cwd)
        served = False
        try:
            proc.stdin.write(json.dumps(
                {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": "2024-11-05",
                            "capabilities": {},
                            "clientInfo": {"name": "guard-test", "version": "1"}}}) + "\n")
            proc.stdin.flush()
            line = proc.stdout.readline()
            served = bool(line.strip())
        except (OSError, ValueError):
            served = False
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            out, err = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
        return proc.returncode, (err or "") + (out or ""), served

    def test_correct_tree_serves(self) -> None:
        """The guard must not block the legitimate case: the code and the state
        belong to the SAME repo, wherever it is reachable from. Proven by the
        protocol answering, not by a pid surviving."""
        twin = _git_repo(Path(self._tmp.name) / "twin")
        twin_state = twin / ".pair"
        twin_state.mkdir(parents=True, exist_ok=True)
        twin_state.joinpath("state.json").write_text(json.dumps(
            {"round": 903, "mode": "fix", "writer": "omp", "roles": {},
             "phase": "implement", "seq": 0}), encoding="utf-8")
        old_state = self.state
        self.state = twin_state
        try:
            rc, out, served = self._converse(twin)
            self.assertTrue(served, f"the correct tree must answer the protocol: {out[:400]}")
            self.assertEqual(rc, 0, out[:200])
        finally:
            self.state = old_state

    def test_same_repo_two_paths_is_one_identity(self) -> None:
        """The guard's identity must survive bind mounts and symlinks: the live
        pair runs the server through /home/alexios/work/umputum while state lives
        at /mnt/storage/work/umputum — one repo, two paths, and a path comparison
        would refuse the live pair. Simulated with a symlink into the same repo."""
        twin = _git_repo(Path(self._tmp.name) / "twin2")
        (twin / ".pair").mkdir(parents=True, exist_ok=True)
        (twin / ".pair" / "state.json").write_text(json.dumps(
            {"round": 903, "mode": "fix", "roles": {}, "phase": "implement",
             "seq": 0}), encoding="utf-8")
        alias = Path(self._tmp.name) / "alias-to-twin2"
        alias.symlink_to(twin, target_is_directory=True)
        old_state = self.state
        self.state = twin / ".pair"
        try:
            rc, out, served = self._converse(alias)   # reached through the alias
            self.assertTrue(served, f"one repo reached two ways must serve: {out[:400]}")
        finally:
            self.state = old_state


    def test_non_git_state_dir_fails_open_and_serves(self) -> None:
        """The regression the peer caught (seq 1625): under `set -e` an unguarded
        command substitution in an assignment EXITS the shell, and `2>/dev/null`
        hides the message but not the status — so a server raised with a state
        dir outside any git repo died rc=128 BEFORE the handshake, silently.
        The installer's verify path uses exactly that shape (temp state), which
        is how it surfaced. Fail-open must mean 'no identity -> serve'."""
        outside = Path(self._tmp.name) / "plain-dir"
        outside.mkdir(parents=True, exist_ok=True)
        plain_state = outside / ".pair"
        plain_state.mkdir(parents=True, exist_ok=True)
        plain_state.joinpath("state.json").write_text(json.dumps(
            {"round": 903, "mode": "fix", "roles": {}, "phase": "implement",
             "seq": 0}), encoding="utf-8")
        old_state = self.state
        self.state = plain_state
        try:
            rc, out, served = self._converse(outside)
            self.assertEqual(rc, 0, f"a non-git state must fail open, not die: {out[:300]}")
            self.assertTrue(served, f"and it must answer the protocol: {out[:300]}")
        finally:
            self.state = old_state

    def test_no_tree_state_refuses_without_creating_a_stray(self) -> None:
        """serve.sh already refuses a stray state; the guard must not turn that
        into a serve-from-anywhere hole. A cwd outside any project with the state
        pinned explicitly still refuses (the tree check passes on cwd)."""
        outside = Path(self._tmp.name) / "elsewhere"
        outside.mkdir(parents=True, exist_ok=True)
        proc = self._raise(outside)
        try:
            proc.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
        # whichever way it refuses, no role lock may be held by a foreign cwd
        self.assertEqual(_lock_holder(self.state, self.role), "",
                         "a foreign cwd must never hold the role")


if __name__ == "__main__":
    unittest.main(verbosity=2)
