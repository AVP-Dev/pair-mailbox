"""Observe healthy standalone startup and process-scoped pair membership."""
from __future__ import annotations

import fcntl
import io
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
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

    def test_exec_wrapper_with_generic_script_retains_owner_identity(self):
        self.tree[10]["argv"] = ["node", "/package/index.js"]
        self.assertEqual(self.enrolled(), self.state)

    def test_generic_script_alias_identifies_nested_cli(self):
        script = Path(self.tmp.name) / "index.js"
        script.touch()
        alias = Path(self.tmp.name) / "codex"
        alias.symlink_to(script)
        self.tree[10]["argv"] = ["node", str(script)]
        self.tree[11] = {"pid": 11, "ppid": 10, "start": "later",
                         "argv": ["node", str(script)]}
        self.tree[12]["ppid"] = 11
        with patch.object(context.shutil, "which", side_effect=lambda name: str(alias) if name == "codex" else None):
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

    @unittest.skipUnless(context.platform.system() == "Darwin",
                         "Darwin libproc/sysctl process identity is platform-specific")
    def test_darwin_process_identity_does_not_execute_ps(self):
        with patch.object(context.subprocess, "run",
                          side_effect=AssertionError("must not execute external process tools")):
            info = context.process_info(os.getpid())
        self.assertIsNotNone(info)
        self.assertEqual(os.getpid(), info["pid"])
        self.assertEqual(os.getppid(), info["ppid"])
        self.assertTrue(info["start"])
        self.assertTrue(info["argv"])

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


class LaunchSecurityBoundary(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pair-launch-boundary-")
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name) / "project"
        self.project.mkdir()
        self.state = self.project / ".pair"
        self.state.mkdir()
        (self.state / "state.json").write_text(json.dumps({"writer": "codex"}))

    def test_codex_writer_uses_workspace_write_auto_review_and_critic_is_read_only(self):
        writer = context.safe_cli_command(
            "codex", "codex", True,
            ["codex", "--no-daemon", "--sandbox", "danger-full-access", "-c", "model=x"])
        self.assertIn("--approve-for-me", writer)
        self.assertNotIn("danger-full-access", writer)
        self.assertIn(["-c", "model=x"], [writer[i:i + 2] for i in range(len(writer) - 1)])
        self.assertIn(["-c", "sandbox_workspace_write.writable_roots=[]"],
                      [writer[i:i + 2] for i in range(len(writer) - 1)])
        self.assertIn(["-c", "sandbox_workspace_write.network_access=false"],
                      [writer[i:i + 2] for i in range(len(writer) - 1)])

        critic = context.safe_cli_command("codex", "codex-critic", False,
                                          ["codex", "--resume", "thread"])
        self.assertIn("read-only", critic)
        self.assertIn("never", critic)
        self.assertNotIn("--approve-for-me", critic)

    def test_pi_trust_is_noninteractive_and_critic_file_tools_are_removed(self):
        writer = context.safe_cli_command(
            "pi", "pi", True,
            ["pi", "--no-approve", "--no-tools", "--tools", "read", "task"])
        self.assertIn("--approve", writer)
        self.assertNotIn("--no-approve", writer)
        self.assertNotIn("--no-tools", writer)
        self.assertNotIn("--tools", writer)

        critic = context.safe_cli_command(
            "pi", "pi-critic", False,
            ["pi", "--approve", "--exclude-tools", "bash,edit,write", "review"])
        self.assertIn("--approve", critic)
        self.assertIn(["--exclude-tools", "edit,write"],
                      [critic[i:i + 2] for i in range(len(critic) - 1)])
        self.assertNotIn("bash,edit,write", critic)

    def test_pi_tool_policy_overrides_require_complete_values(self):
        for option in ("--tools", "--exclude-tools"):
            with self.subTest(option=option):
                with self.assertRaisesRegex(ValueError, "needs a value"):
                    context.safe_cli_command("pi", "pi", True, ["pi", option])

    def test_every_profile_refuses_bypass_flags_and_wrong_executable(self):
        for profile in ("agy", "codex", "claude", "opencode", "pi", "omp"):
            with self.subTest(profile=profile):
                with self.assertRaisesRegex(ValueError, "unsafe permission/sandbox bypass"):
                    context.safe_cli_command(profile, profile, True,
                                             [profile, "--dangerously-skip-permissions"])
        for profile, flag in (("claude", "--allow-dangerously-skip-permissions"),
                              ("omp", "--auto-approve"), ("omp", "--approval-mode=yolo")):
            with self.subTest(profile=profile, flag=flag):
                with self.assertRaises(ValueError):
                    context.safe_cli_command(profile, profile, True, [profile, flag])
        with self.assertRaisesRegex(ValueError, "must execute"):
            context.safe_cli_command("codex", "codex", True, ["sh", "-c", "codex"])

    def test_codex_security_config_and_extra_write_roots_cannot_be_overridden(self):
        for override in (
            ["-c", 'sandbox_mode="danger-full-access"'],
            ["-c", 'sandbox_workspace_write.writable_roots=["/tmp"]'],
            ["--config=sandbox_workspace_write.network_access=true"],
        ):
            with self.subTest(override=override):
                with self.assertRaisesRegex(ValueError, "sandbox/approval config"):
                    context.safe_cli_command("codex", "codex", True, ["codex", *override])

    def test_codex_cwd_add_dir_and_worktree_overrides_are_removed(self):
        command = context.safe_cli_command(
            "codex", "codex", True,
            ["codex", "--cd", "/tmp", "--add-dir=/", "--worktree", "--resume", "thread"])
        self.assertNotIn("/tmp", command)
        self.assertNotIn("/", command)
        self.assertNotIn("--worktree", command)
        self.assertIn(["--resume", "thread"],
                      [command[i:i + 2] for i in range(len(command) - 1)])

    def test_critic_launch_fails_closed_without_a_supported_os_backend(self):
        with self.assertRaisesRegex(ValueError, "no OS read-only backend"):
            context.role_sandbox_argv("agy-critic", self.state, ["agy"],
                                      workspace=self.project, system="Plan9")

    def test_preflight_detaches_existing_hardlink_aliases(self):
        target = self.project / "tracked.txt"
        target.write_text("original", encoding="utf-8")
        alias = Path(self.tmp.name) / "outside-alias.txt"
        os.link(target, alias)
        original_inode = target.stat().st_ino

        context._break_external_hardlinks([self.project], self.project)

        self.assertEqual(target.read_text(encoding="utf-8"), "original")
        self.assertEqual(target.stat().st_nlink, 1)
        self.assertNotEqual(target.stat().st_ino, original_inode)
        alias.write_text("changed outside", encoding="utf-8")
        self.assertEqual(target.read_text(encoding="utf-8"), "original")

    def test_claude_and_agy_modes_are_role_specific_and_safe(self):
        writer = context.safe_cli_command("claude", "claude", True,
                                          ["claude", "--resume", "abc", "--add-dir", "/tmp"])
        self.assertEqual(writer[-2:], ["--permission-prompts", "none"])
        settings = json.loads(writer[writer.index("--settings") + 1])
        self.assertTrue(settings["sandbox"]["enabled"])
        self.assertTrue(settings["sandbox"]["failIfUnavailable"])
        self.assertFalse(settings["sandbox"]["allowUnsandboxedCommands"])
        self.assertNotIn("/tmp", writer)

        critic = context.safe_cli_command("claude", "claude-critic", False, ["claude"])
        self.assertIn(["--permission-mode", "dontAsk"],
                      [critic[i:i + 2] for i in range(len(critic) - 1)])
        self.assertEqual(critic[-2:], ["--permission-prompts", "none"])

        agy = context.safe_cli_command("agy", "agy", True, ["agy", "--add-dir=/tmp"])
        self.assertEqual(agy[-2:], ["--mode=auto", "--sandbox"])
        self.assertNotIn("/tmp", agy)
        with self.assertRaisesRegex(ValueError, "always-proceed"):
            context.safe_cli_command("agy", "agy", True,
                                     ["agy", "--mode=always-proceed"])

    def test_omp_uses_write_mode_and_rejects_config_overlay(self):
        command = context.safe_cli_command("omp", "omp", True,
                                           ["omp", "--cwd=/", "--approval-mode=always-ask"])
        self.assertEqual(command[-1], "--approval-mode=write")
        self.assertNotIn("/", command)
        with self.assertRaisesRegex(ValueError, "config overlays"):
            context.safe_cli_command("omp", "omp", True, ["omp", "--config=tools.approvalMode=yolo"])

    def test_opencode_auto_precedes_the_positional_prompt(self):
        prompt = "write a report"
        command = context.safe_cli_command(
            "opencode", "opencode", True,
            ["opencode", "run", "--standalone", "--auto", prompt])
        self.assertEqual(command[-1], prompt)
        self.assertLess(command.index("--auto"), len(command) - 1)

    @unittest.skipUnless(context.platform.system() == "Darwin" and
                         context.shutil.which("sandbox-exec"),
                         "macOS Seatbelt is required for the live access test")
    def test_critic_cannot_write_project_through_symlink_or_hardlink(self):
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        target = self.project / "source.txt"
        target.write_text("original", encoding="utf-8")
        alias = Path(self.tmp.name) / "alias.txt"
        alias.symlink_to(target)
        old_hardlink = Path(self.tmp.name) / "old-hardlink.txt"
        os.link(target, old_hardlink)
        new_hardlink = Path(self.tmp.name) / "new-hardlink.txt"
        external = Path(self.tmp.name) / "outside.txt"
        code = (
            "from pathlib import Path; import os, sys; "
            "symlink_denied=False; "
            "\ntry: Path(sys.argv[1]).write_text('changed through symlink')\n"
            "except OSError: symlink_denied=True\n"
            "hardlink_create_denied=False; "
            "\ntry: os.link(sys.argv[3], sys.argv[4])\n"
            "except OSError: hardlink_create_denied=True\n"
            "Path(sys.argv[2]).write_text('changed through old alias')\n"
            "Path(sys.argv[5]).write_text('allowed outside project')\n"
            "print(symlink_denied, hardlink_create_denied)"
        )
        argv = context.role_sandbox_argv(
            "agy-critic", self.state,
            [sys.executable, "-c", code, str(alias), str(old_hardlink),
             str(target), str(new_hardlink), str(external)],
            workspace=self.project, system="Darwin")
        result = subprocess.run(argv, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "True True")
        self.assertEqual(target.read_text(encoding="utf-8"), "original")
        self.assertEqual(old_hardlink.read_text(encoding="utf-8"), "changed through old alias")
        self.assertEqual(external.read_text(encoding="utf-8"), "allowed outside project")

    @unittest.skipUnless(context.platform.system() == "Darwin" and
                         context.shutil.which("sandbox-exec"),
                         "macOS Seatbelt is required for the live socket test")
    def test_critic_can_use_brokered_mcp_socket_inside_read_only_state_path(self):
        os.chmod(self.state, 0o700)
        socket_path = Path(context.prepare_role_socket_path(self.state, "agy-critic"))
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.bind(str(socket_path))
        self.addCleanup(lambda: socket_path.unlink(missing_ok=True))
        listener.listen(1)
        listener.settimeout(10)

        received = []

        def serve_one():
            try:
                connection, _ = listener.accept()
                with connection:
                    received.append(connection.recv(4))
                    connection.sendall(b"pong")
            except OSError as exc:
                received.append(str(exc).encode())

        worker = threading.Thread(target=serve_one, daemon=True)
        worker.start()
        code = (
            "import socket,sys; c=socket.socket(socket.AF_UNIX); "
            "c.connect(sys.argv[1]); c.sendall(b'ping'); "
            "print(c.recv(4).decode())"
        )
        argv = context.role_sandbox_argv(
            "agy-critic", self.state, [sys.executable, "-c", code, str(socket_path)],
            workspace=self.project, system="Darwin")
        result = subprocess.run(argv, capture_output=True, text=True, timeout=20)
        worker.join(timeout=2)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "pong")
        self.assertEqual(received, [b"ping"])

    @unittest.skipUnless(context.platform.system() == "Linux" and context.shutil.which("bwrap"),
                         "Linux bubblewrap is required for the live access test")
    def test_critic_cannot_write_project_or_link_around_bwrap_read_only_mount(self):
        bwrap = context.shutil.which("bwrap")
        probe = subprocess.run([bwrap, "--ro-bind", "/", "/", "--", "/bin/true"],
                               capture_output=True, text=True, timeout=10)
        if probe.returncode:
            self.skipTest(f"bubblewrap namespaces unavailable: {probe.stderr.strip()}")
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        target = self.project / "source.txt"
        target.write_text("original", encoding="utf-8")
        old_alias = Path(self.tmp.name) / "old-alias.txt"
        os.link(target, old_alias)
        new_alias = Path(self.tmp.name) / "new-alias.txt"
        code = (
            "from pathlib import Path; import os, sys; "
            "write_denied=False; link_denied=False; "
            "\ntry: Path(sys.argv[1]).write_text('changed')\n"
            "except OSError: write_denied=True\n"
            "try: os.link(sys.argv[1], sys.argv[3])\n"
            "except OSError: link_denied=True\n"
            "Path(sys.argv[2]).write_text('changed through old alias')\n"
            "print(write_denied, link_denied)"
        )
        argv = context.role_sandbox_argv(
            "agy-critic", self.state,
            [sys.executable, "-c", code, str(target), str(old_alias), str(new_alias)],
            workspace=self.project, system="Linux")
        fd = int(argv[argv.index("--seccomp") + 1])
        try:
            result = subprocess.run(argv, pass_fds=(fd,), capture_output=True,
                                    text=True, timeout=20)
        finally:
            os.close(fd)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "True True")
        self.assertEqual(target.read_text(encoding="utf-8"), "original")
        self.assertEqual(old_alias.read_text(encoding="utf-8"), "changed through old alias")


if __name__ == "__main__":
    unittest.main()
