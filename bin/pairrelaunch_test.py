#!/usr/bin/env python3
"""Project-scoped, resumable CLI restart tests."""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "bin" / "pairrelaunch"


def load_relauncher():
    loader = SourceFileLoader("pairrelaunch_tested", str(SCRIPT))
    spec = importlib.util.spec_from_loader("pairrelaunch_tested", loader)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RelaunchTests(unittest.TestCase):
    def test_codex_uses_saved_session_resume_command(self) -> None:
        relaunch = load_relauncher()
        calls = []
        fake_context = SimpleNamespace(
            profile_for=lambda role: role,
            launch_shell=lambda role, state, command: calls.append(
                (role, state, command)) or "wrapped command")
        with mock.patch.dict(sys.modules, {"launch_context": fake_context}):
            self.assertEqual("wrapped command", relaunch.launch_cmd("codex"))
        self.assertEqual("codex resume --last --no-daemon", calls[0][2])

    def test_other_profiles_keep_their_normal_launch_command(self) -> None:
        relaunch = load_relauncher()
        calls = []
        fake_context = SimpleNamespace(
            profile_for=lambda role: role,
            launch_shell=lambda role, state, command: calls.append(command) or "wrapped")
        with mock.patch.dict(sys.modules, {"launch_context": fake_context}):
            self.assertEqual("wrapped", relaunch.launch_cmd("agy"))
        self.assertEqual(["agy --mode=auto --sandbox"], calls)

    def test_restart_uses_explicit_shared_state_and_scoped_inventory(self) -> None:
        relaunch = load_relauncher()
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / ".pair"
            state.mkdir()
            (state / "state.json").write_text(json.dumps({"writer": "codex"}),
                                                encoding="utf-8")
            with mock.patch.object(relaunch, "pane_of", return_value="%0"), \
                    mock.patch.object(relaunch, "launch_cmd", return_value="launch"), \
                    mock.patch.object(relaunch, "server_pid", return_value="77"), \
                    mock.patch.object(sys, "argv", [str(SCRIPT), "--role", "codex",
                                                     "--state", str(state), "--check"]):
                self.assertEqual(0, relaunch.main())
            self.assertEqual(state.resolve(), relaunch.STATE)

    def test_server_pid_uses_shared_exact_project_inventory(self) -> None:
        relaunch = load_relauncher()
        state = Path("/tmp/project with spaces/.pair")
        relaunch.STATE = state
        scanner = mock.Mock(return_value=[{"pid": 82, "role": "codex", "start": 1.0}])
        relaunch.pair_mailbox_servers = scanner
        self.assertEqual("82", relaunch.server_pid("codex"))
        scanner.assert_called_once_with(state, "codex")

    def test_pane_selection_uses_state_pane_and_project_path(self) -> None:
        relaunch = load_relauncher()
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / ".pair"
            state.mkdir()
            (state / "state.json").write_text(json.dumps({
                "roles": {"codex": {"pane_uid": "%wanted"}}}), encoding="utf-8")
            rows = ("%foreign|other|0|codex|/tmp/another-project\n"
                    f"%other|same|0|codex|{ROOT}\n"
                    f"%wanted|same|1|codex|{ROOT}\n")
            relaunch.STATE = state
            relaunch.tmux = lambda *args: (0, rows)
            self.assertEqual("%wanted", relaunch.pane_of("codex"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
