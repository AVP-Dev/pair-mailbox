"""Observe healthy standalone startup and process-scoped pair membership."""
from __future__ import annotations

import fcntl
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import launch_context as context

ROOT = Path(__file__).resolve().parents[2]
PKG = ROOT / "mcp/pair_mailbox"


def frames():
    return "\n".join(json.dumps(item) for item in (
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "clientInfo": {"name": "test"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    )) + "\n"


class StandaloneStartup(unittest.TestCase):
    def test_initialize_and_empty_catalog(self):
        output = io.StringIO()
        context.standalone(io.StringIO(frames()), output)
        replies = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertIn("Standalone CLI", replies[0]["result"]["instructions"])
        self.assertEqual(replies[1]["result"], {"tools": []})

    def test_global_entries_ignore_existing_state_and_occupied_role(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            state = project / ".pair"
            state.mkdir()
            original = '{"writer":"agy","roles":{},"round":7}'
            (state / "state.json").write_text(original)
            with (state / ".server-codex.lock").open("w") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                for command in (["sh", str(PKG / "serve.sh"), "codex"],
                                [sys.executable, str(PKG / "mcp_socket_client.py"), "codex"]):
                    env = dict(os.environ, PAIR_STATE=str(state), PAIR_ROLE="agy",
                               PAIR_LAUNCH_PID="999999999", PAIR_LAUNCH_START="stale")
                    result = subprocess.run(command, cwd=project, env=env, input=frames(),
                                            capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    replies = [json.loads(line) for line in result.stdout.splitlines()]
                    self.assertEqual(replies[-1]["result"], {"tools": []})
                    self.assertIn("Standalone CLI", replies[0]["result"]["instructions"])
            self.assertEqual((state / "state.json").read_text(), original)
            self.assertEqual(sorted(p.name for p in state.iterdir()),
                             [".server-codex.lock", "state.json"])

    def test_new_directory_does_not_acquire_pair_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(["sh", str(PKG / "serve.sh"), "codex"], cwd=tmp,
                                    input=frames(), capture_output=True, text=True, timeout=10,
                                    env={k: v for k, v in os.environ.items() if not k.startswith("PAIR_")})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('"tools": []', result.stdout)
            self.assertEqual(list(Path(tmp).iterdir()), [])


class LaunchMembership(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name).resolve() / ".pair"
        self.state.mkdir()
        (self.state / "state.json").write_text("{}")
        self.env = {"PAIR_LAUNCH_PID": "10", "PAIR_LAUNCH_START": "birth",
                    "PAIR_LAUNCH_PROFILE": "codex", "PAIR_STATE": str(self.state)}
        self.tree = {10: {"pid": 10, "ppid": 1, "start": "birth", "argv": ["codex"]},
                     12: {"pid": 12, "ppid": 10, "start": "later", "argv": ["python3", "serve.py"]}}

    def enrolled(self, role="codex"):
        with patch.object(context, "process_info", side_effect=self.tree.get), \
                patch.object(context.os, "getpid", return_value=12):
            return context.enrolled_state(role, self.env)

    def test_enrolled_parent_is_recognized(self):
        self.assertEqual(self.enrolled(), self.state)

    def test_nested_same_cli_and_different_cli_are_standalone(self):
        for name in ("codex", "agy"):
            self.tree[11] = {"pid": 11, "ppid": 10, "start": "later", "argv": [name]}
            self.tree[12]["ppid"] = 11
            self.assertIsNone(self.enrolled())

    def test_recycled_pid_is_not_membership(self):
        self.tree[10]["start"] = "new birth"
        self.assertIsNone(self.enrolled())

    def test_socket_role_cannot_select_another_profile_or_escape_path(self):
        for role in ("agy", "codex-../writer", "codex/bad"):
            with patch.dict(os.environ, {"PAIR_ROLE": role}):
                with self.assertRaisesRegex(ValueError, "paired role"):
                    context.configured_role("codex")
        with patch.dict(os.environ, {"PAIR_ROLE": "codex-critic"}):
            self.assertEqual(context.configured_role("codex"), "codex-critic")

    def test_enrolled_profile_mismatch_is_not_a_solo_fallback(self):
        with self.assertRaisesRegex(ValueError, "paired CLI profile"):
            self.enrolled("agy")

    def actual_launcher(self, socket_backend=False):
        cli = Path(self.tmp.name) / "codex"
        command = ([sys.executable, str(PKG / "mcp_socket_client.py"), "codex"]
                   if socket_backend else ["sh", str(PKG / "serve.sh"), "codex"])
        cli.write_text("#!" + sys.executable + "\n" +
            "import json, subprocess, sys\n" +
            "command = " + repr(command) + "\n" +
            "request = " + repr(frames()) + "\n" +
            "reply = subprocess.run(command, input=request, text=True, capture_output=True, check=True)\n" +
            "answer = json.loads(reply.stdout.splitlines()[0])\n" +
            "print(json.dumps(answer))\n" +
            "if 'nested' not in sys.argv[1:]:\n" +
            "    child = subprocess.run([sys.executable, __file__, 'nested'], text=True, capture_output=True, check=True)\n" +
            "    print(child.stdout.strip())\n")
        cli.chmod(0o755)
        # Leave the existing operator's environment intact outside this child.
        if socket_backend:
            def stop_owned_daemon():
                roles = json.loads((self.state / "state.json").read_text()).get("roles", {})
                pid = roles.get("codex", {}).get("pid")
                if pid:
                    try:
                        os.kill(pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
            self.addCleanup(stop_owned_daemon)
        result = subprocess.run(context.launch_argv("codex", self.state, [str(cli)]),
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        replies = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertIn("two-agent collaboration", replies[0]["result"]["instructions"])
        self.assertIn("Standalone CLI", replies[1]["result"]["instructions"])
        roles = json.loads((self.state / "state.json").read_text())["roles"]
        self.assertEqual(list(roles), ["codex"])
        if socket_backend:
            self.assertEqual(json.loads((self.state / "state.json").read_text())["transport"], "socket")

    def test_actual_launcher_enrolls_parent_but_not_nested_cli(self):
        self.actual_launcher()

    def test_enrolled_socket_parent_starts_daemon_but_nested_cli_does_not_join(self):
        self.actual_launcher(socket_backend=True)


if __name__ == "__main__":
    unittest.main()
