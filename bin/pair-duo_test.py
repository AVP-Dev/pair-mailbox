#!/usr/bin/env python3
"""The duo launcher: two instances of ONE CLI must get two DIFFERENT roles.

The measured shape: `bin/pair-duo` names two roles ("<cli>-writer" and
"<cli>-critic"), but the role in a pane used to be whatever the CLI's MCP
config said — and the config is per CLI, so both instances read the SAME file,
claimed the SAME role, and the second was refused by the "one live role means
one live process" rule (server.py). The launcher now exports the per-instance
role, and serve.sh accepts an instance of the configured role (and only that).

The measurement here is the ENVIRONMENT the CLI actually runs in, not the
script's text: a stub CLI writes down the role it was launched with, exactly
the way the real MCP server inherits it.

Run: python3 bin/pair-duo_test.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DUO = ROOT / "bin" / "pair-duo"
CLI = "agy"  # a real profile name: the duo's roles are derived from it


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=str(path), check=True, timeout=30)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=str(path), check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(path), check=True)
    return path


class DuoRoleCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.project = _git_repo(base / "proj")
        self.state = self.project / ".pair"
        self.state.mkdir(parents=True)
        (self.state / "state.json").write_text(json.dumps(
            {"round": 1, "mode": "fix", "writer": None, "phase": "implement",
             "roles": {}, "tasks": [], "seq": 0}), encoding="utf-8")
        # The stub CLI: it reports the role it was launched with and stays alive,
        # so the panes do not die before the roles are read.
        self.report = base / "roles.txt"
        self.stub_dir = base / "bin"
        self.stub_dir.mkdir()
        stub = self.stub_dir / CLI
        stub.write_text(
            "#!/bin/sh\n"
            f'printf "%s\\n" "${{PAIR_ROLE:-}}" >> "{self.report}"\n'
            "sleep 60\n",
            encoding="utf-8")
        stub.chmod(0o755)
        self.session = f"duo-test-{os.getpid()}"

    def tearDown(self) -> None:
        subprocess.run(["tmux", "kill-session", "-t", self.session],
                       capture_output=True, timeout=20)
        self._tmp.cleanup()

    def _run_duo_in_tmux(self) -> None:
        """Run the duo inside a tmux session: two panes, one stub CLI each."""
        env = dict(os.environ)
        env["PATH"] = f"{self.stub_dir}:{env['PATH']}"
        command = (
            f"cd '{self.project}' && PAIR_STATE='{self.state}' "
            f"'{DUO}' {CLI} >'{self.state}/duo.log' 2>&1; sleep 60")
        subprocess.run(["tmux", "new-session", "-d", "-s", self.session,
                        "-c", str(self.project), "sh", "-c", command],
                       capture_output=True, timeout=30, env=env)

    def _roles(self, wait: float = 20.0) -> list[str]:
        deadline = time.time() + wait
        while time.time() < deadline:
            if self.report.exists():
                lines = [line.strip() for line in
                         self.report.read_text(encoding="utf-8").splitlines()
                         if line.strip()]
                if len(lines) >= 2:
                    return lines
            time.sleep(0.2)
        return []

    def test_the_two_panes_get_two_different_roles(self) -> None:
        """The whole point: one CLI, two instances, two identities.

        Without the per-instance export both panes would carry the config's
        role, the second server would be refused by the role-collision rule,
        and the critic pane would have no pair tools at all.
        """
        self._run_duo_in_tmux()
        roles = self._roles()
        self.assertEqual(2, len(roles),
                         f"both panes must report a role, got {roles}")
        self.assertNotEqual(roles[0], roles[1],
                            "two instances of one CLI must not share one role")
        for role in roles:
            self.assertTrue(role.startswith(f"{CLI}"),
                            f"the role must extend the configured one: {role!r}")
            self.assertIn(role, (f"{CLI}-writer", f"{CLI}-critic"),
                          f"the duo names its two roles, got {role!r}")


class DuoStateResolutionCase(unittest.TestCase):
    """The room belongs to the project, never to the inherited environment.

    Round 126 measured the defect live: an operator standing in /tmp/myproj
    ran `bin/pair-duo opencode`; the script took PAIR_STATE on its face, wrote
    a WRITER into that foreign room, and launched the panes bound to it. The
    default was the install directory, so the pair served pair-mailbox itself
    instead of the project the operator was standing in.

    Round 121 fixed the same class in bin/pairup; this is the third entry
    point into the same room, and it was not covered.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.project = _git_repo(base / "proj")
        self.state = self.project / ".pair"
        self.state.mkdir(parents=True)
        (self.state / "state.json").write_text(json.dumps(
            {"round": 1, "mode": "fix", "writer": None, "phase": "implement",
             "roles": {}, "tasks": [], "seq": 0}), encoding="utf-8")
        # A foreign room, with a room of its own.
        self.foreign = base / "foreign" / ".pair"
        self.foreign.mkdir(parents=True)
        (self.foreign / "state.json").write_text(json.dumps(
            {"round": 7, "mode": "fix", "writer": None, "phase": "done",
             "roles": {}, "tasks": [], "seq": 0}), encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _resolve(self, cwd: Path, env: dict) -> tuple[str, str, str]:
        """What the REAL launcher resolves, without launching anything.

        `--print-state` prints the room and the project and stops: the
        resolution is the first thing the script does, and it is what the
        defect was about. The script under test is the shipped one — a copy
        of its logic would pass while the script kept the defect.
        """
        proc = subprocess.run([str(DUO), "--print-state", CLI], cwd=str(cwd),
                              capture_output=True, text=True, timeout=30, env=env)
        out = {}
        for line in proc.stdout.splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                out[key] = value
        self.assertIn("state", out,
                      f"the resolution did not run: {proc.stdout} {proc.stderr}")
        return out.get("state", ""), out.get("project", ""), proc.stderr

    def test_a_foreign_inherited_state_is_refused(self) -> None:
        """The defect: the foreign room must not be taken on its face."""
        env = dict(os.environ)
        env["PAIR_STATE"] = str(self.foreign)
        state, _, stderr = self._resolve(self.project, env)
        self.assertNotEqual(state, str(self.foreign),
                            "the foreign room must not be adopted")
        self.assertIn("ignoring PAIR_STATE", stderr,
                      "the refusal must be loud, not a quiet repoint")
        self.assertIn(str(self.foreign), stderr,
                      "the refusal must name the room it refused")
        self.assertIn(str(self.state), stderr,
                      "the refusal must name the room it used instead")

    def test_the_foreign_room_is_not_written_to(self) -> None:
        """The writer is what the defect cost: the foreign room gained a
        `writer` and a role row."""
        env = dict(os.environ)
        env["PAIR_STATE"] = str(self.foreign)
        before = (self.foreign / "state.json").read_text(encoding="utf-8")
        self._resolve(self.project, env)
        after = (self.foreign / "state.json").read_text(encoding="utf-8")
        self.assertEqual(before, after,
                         "the foreign room must be byte-identical afterwards")

    def test_the_room_is_the_callers_project(self) -> None:
        """With no inherited state, the room is the one underfoot."""
        env = dict(os.environ)
        env.pop("PAIR_STATE", None)
        state, project, _ = self._resolve(self.project, env)
        self.assertEqual(str(self.state), state,
                         f"the room must be the caller's own, got {state}")
        self.assertEqual(str(self.project), project,
                         f"the panes must stand in the caller's project, got {project}")

    def test_the_project_is_never_the_install_dir(self) -> None:
        """The default used to be the install directory, so the pair served
        pair-mailbox instead of the project the operator was standing in."""
        env = dict(os.environ)
        env.pop("PAIR_STATE", None)
        _, project, _ = self._resolve(self.project, env)
        self.assertNotEqual(str(ROOT), project,
                            "the project must not default to the install dir")

    def test_trust_env_keeps_the_foreign_room_on_purpose(self) -> None:
        """The deliberate escape: an operator who KNOWS which room they want
        says so, and the script obeys."""
        env = dict(os.environ)
        env["PAIR_STATE"] = str(self.foreign)
        env["PAIR_TRUST_ENV"] = "1"
        state, _, _ = self._resolve(self.project, env)
        self.assertEqual(str(self.foreign), state,
                         "PAIR_TRUST_ENV=1 must keep the room the operator named")

    def test_an_explicit_state_flag_beats_the_environment(self) -> None:
        """The flag is the operator speaking; the environment is a leftover."""
        env = dict(os.environ)
        env["PAIR_STATE"] = str(self.foreign)
        proc = subprocess.run([str(DUO), "--state", str(self.state),
                               "--print-state", CLI],
                              cwd=str(self.project), capture_output=True,
                              text=True, timeout=30, env=env)
        self.assertIn(f"state={self.state}", proc.stdout,
                      f"an explicit --state must win: {proc.stdout} {proc.stderr}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
