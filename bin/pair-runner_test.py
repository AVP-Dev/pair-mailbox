#!/usr/bin/env python3
"""bin/pair-runner: the runner starts an agent without tmux, and says so.

Three claims, each measured on the real thing rather than on a copy of it:

  1. the per-launch MCP configuration names THIS role and THIS room;
  2. the child is a subprocess with a pane-free environment (the pane
     variables are removed, the ancestry discovery is switched off) and the
     configuration injected inline;
  3. the mailbox carries a letter between two real roles with no tmux in the
     environment at all: `pair_send` lands it, `pair_read` serves it, the
     `ack_token` closes the batch.

The third one runs the real `serve.sh` against a real state dir in a real git
project, because the claim is about the room's own servers, not about a mock.

Run: python3 bin/pair-runner_test.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNNER = ROOT / "bin" / "pair-runner"
SERVE = ROOT / "mcp" / "pair_mailbox" / "serve.sh"


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=str(path), check=True, timeout=30)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=str(path), check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(path), check=True)
    return path


def _clean_env() -> dict:
    """An environment with no tmux, no inherited room, no inherited role.

    The pane variables are what would make a server believe it lives in a
    terminal; the ancestry knob stops it from finding one by walking the
    process tree.
    """
    env = {k: v for k, v in os.environ.items()
           if k not in ("TMUX", "TMUX_PANE", "PAIR_PANE", "PAIR_PANE_SOCKET",
                        "PAIR_STATE", "PAIR_ROLE", "PAIR_PILOT_PANE")}
    env["PAIR_DISABLE_ANCESTRY_PANE"] = "1"
    env["PAIR_DISABLE_RING"] = "1"
    return env


class RunnerCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.room = self.base / "room"
        self.room.mkdir(parents=True)
        (self.room / "state.json").write_text(json.dumps(
            {"round": 144, "mode": "fix", "writer": None, "phase": "implement",
             "roles": {}, "tasks": [], "seq": 0}), encoding="utf-8")
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

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(RUNNER), *args],
                              cwd=str(ROOT), capture_output=True, text=True,
                              timeout=120, env=_clean_env())


class TestRunnerConfig(RunnerCase):
    def test_the_config_names_the_role_and_the_room(self) -> None:
        proc = self._run("--role", "runner-role", "--state", str(self.room),
                         "--print-config")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        config = json.loads(proc.stdout)
        entry = config["mcp"]["pair"]
        self.assertEqual(entry["command"], [
            "sh", str(SERVE), "runner-role", "--state", str(self.room.resolve())])
        self.assertTrue(entry["enabled"])
        self.assertEqual(entry["timeout"], 65000)

    def test_an_unsupported_cli_is_refused_by_name(self) -> None:
        proc = self._run("--role", "runner-role", "--state", str(self.room),
                         "--cli", "claude")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("claude", proc.stderr)
        self.assertIn("unsupported", proc.stderr.lower())

    def test_a_project_with_its_own_engine_is_refused(self) -> None:
        """The engine and the room must agree before a child is started."""
        project = _git_repo(self.base / "proj")
        package = project / "mcp" / "pair_mailbox"
        package.mkdir(parents=True)
        (package / "serve.sh").write_text("#!/bin/sh\n", encoding="utf-8")
        room = project / ".pair"
        room.mkdir()
        (room / "state.json").write_text(json.dumps(
            {"round": 144, "mode": "fix", "writer": None, "phase": "implement",
             "roles": {}, "tasks": [], "seq": 0}), encoding="utf-8")
        proc = self._run("--role", "runner-role", "--state", str(room),
                         "--print-config")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("own pair-mailbox", proc.stderr)


class TestRunnerChild(RunnerCase):
    def test_the_child_is_a_subprocess_with_a_pane_free_environment(self) -> None:
        record = self.base / "child.json"
        stub = self.base / "stub-cli"
        stub.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            f"json.dump({{'argv': sys.argv, 'env': dict(os.environ)}}, "
            f"open({str(record)!r}, 'w'))\n",
            encoding="utf-8")
        stub.chmod(0o755)
        proc = self._run("--role", "runner-role", "--state", str(self.room),
                         "--cli-bin", str(stub), "--message", "hello runner")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        seen = json.loads(record.read_text(encoding="utf-8"))
        self.assertIn("hello runner", seen["argv"], seen["argv"])
        env = seen["env"]
        for name in ("TMUX", "TMUX_PANE", "PAIR_PANE", "PAIR_STATE", "PAIR_ROLE"):
            self.assertNotIn(name, env, f"the child must not inherit {name}")
        self.assertEqual(env["PAIR_DISABLE_ANCESTRY_PANE"], "1")
        config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(config["mcp"]["pair"]["command"][2], "runner-role")
        self.assertEqual(config["mcp"]["pair"]["command"][4], str(self.room.resolve()))


    def test_a_named_model_reaches_the_command_line(self) -> None:
        """The model is named, not inherited: the host's default refused the
        request on this host, and a launch that cannot say which model answers
        it fails for reasons the operator cannot see."""
        record = self.base / "child.json"
        stub = self.base / "stub-cli"
        stub.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            f"json.dump({{'argv': sys.argv, 'env': dict(os.environ)}}, "
            f"open({str(record)!r}, 'w'))\n",
            encoding="utf-8")
        stub.chmod(0o755)
        proc = self._run("--role", "runner-role", "--state", str(self.room),
                         "--cli-bin", str(stub), "--model", "provider/model",
                         "--message", "hello runner")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        argv = json.loads(record.read_text(encoding="utf-8"))["argv"]
        self.assertIn("-m", argv)
        self.assertEqual(argv[argv.index("-m") + 1], "provider/model")


class Server:
    """One real pair server, spoken to over stdio JSON-RPC."""

    def __init__(self, role: str, room: Path, cwd: Path) -> None:
        self.role = role
        self.proc = subprocess.Popen(
            ["sh", str(SERVE), role, "--state", str(room)], cwd=str(cwd),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=_clean_env())
        self._id = 0

    def call(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        request = {"jsonrpc": "2.0", "id": self._id, "method": method,
                   "params": params or {}}
        self.proc.stdin.write(json.dumps(request) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line.strip():
            raise AssertionError(
                f"the server {self.role} answered nothing: {self.proc.stderr.read()[:400]}")
        return json.loads(line)

    def tool(self, name: str, arguments: dict) -> dict:
        answer = self.call("tools/call", {"name": name, "arguments": arguments})
        result = answer.get("result", {})
        if result.get("isError"):
            raise AssertionError(f"{name} refused: {result}")
        return json.loads(result["content"][0]["text"])

    def initialize(self) -> None:
        self.call("initialize", {"protocolVersion": "2024-11-05",
                                 "capabilities": {},
                                 "clientInfo": {"name": "runner-test", "version": "1"}})

    def close(self) -> None:
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        for stream in (self.proc.stdout, self.proc.stderr):
            try:
                stream.close()
            except OSError:
                pass


class TestPassiveDelivery(RunnerCase):
    def test_a_letter_crosses_the_room_without_tmux(self) -> None:
        """The guarantee the runner rests on: the mailbox carries the letter.

        A real git project, a real state, the real `serve.sh`, and no tmux
        anywhere in the environment. The writer sends, the reader reads the
        batch, and the `ack_token` closes it.
        """
        project = _git_repo(self.base / "proj")
        room = project / ".pair"
        room.mkdir()
        (room / "state.json").write_text(json.dumps(
            {"round": 144, "mode": "fix", "writer": None, "phase": "implement",
             "roles": {}, "tasks": [], "seq": 0}), encoding="utf-8")
        # The sanctioned engine: this project runs the engine from another
        # tree, and serve.sh's origin guard wants the record that says so.
        (room / "engine").write_text(str(ROOT), encoding="utf-8")

        reader = Server("runner-reader", room, project)
        writer = Server("runner-writer", room, project)
        self.procs.extend([reader.proc, writer.proc])
        try:
            reader.initialize()
            writer.initialize()

            sent = writer.tool("pair_send", {"to": "runner-reader",
                                              "subject": "runner probe",
                                              "text": "a letter with no tmux in it"})
            self.assertTrue(sent["delivered"])
            self.assertTrue(sent["target_is_registered"])

            batch = reader.tool("pair_read", {"wait_seconds": 5})
            self.assertTrue(batch["must_ack"])
            texts = [m["text"] for m in batch["messages"]]
            self.assertIn("a letter with no tmux in it", texts)

            acked = reader.tool("pair_read", {"ack_token": batch["ack_token"]})
            self.assertFalse(acked["must_ack"])
        finally:
            reader.close()
            writer.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
