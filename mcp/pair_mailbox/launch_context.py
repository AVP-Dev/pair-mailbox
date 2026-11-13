"""Opt in one CLI process to pairing; ordinary global MCP starts stay inert."""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PREFIX = "PAIR_LAUNCH_"
MCP_ENV_VARS = ["PAIR_LAUNCH_PID", "PAIR_LAUNCH_START", "PAIR_LAUNCH_PROFILE",
                "PAIR_LAUNCH_ENGINE", "PAIR_STATE", "PAIR_ROLE", "PAIR_PANE_SOCKET",
                "PAIR_CLIENT_TIMEOUT", "PAIR_SOCKET_REDIAL", "TMUX", "TMUX_PANE"]
MCP_ENV_VARS += ["PAIR_TRUNK", "PAIR_MERGE_STRATEGY", "PAIR_WORKTREE_SHARE"]


def profile_for(role: str) -> str:
    profiles = json.loads((ROOT / "cli-profiles.json").read_text(encoding="utf-8"))
    return next((name for name in sorted(profiles, key=len, reverse=True)
                 if not name.startswith("_") and
                 (role == name or role.startswith((name + "-", name + "_")))), "")


def process_info(pid: int) -> dict | None:
    try:
        proc = Path("/proc") / str(pid)
        if proc.is_dir():
            fields = (proc / "stat").read_text().rpartition(")")[2].split()
            argv = (proc / "cmdline").read_bytes().decode(errors="replace").split("\0")
            return {"pid": pid, "ppid": int(fields[1]), "start": fields[19],
                    "argv": [arg for arg in argv if arg]}
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "ppid=", "-o", "lstart=", "-o", "args="],
            capture_output=True, text=True, timeout=2)
        fields = result.stdout.strip().split(None, 6)
        if result.returncode == 0 and len(fields) == 7:
            return {"pid": pid, "ppid": int(fields[0]),
                    "start": " ".join(fields[1:6]), "argv": fields[6].split(None, 2)[:2]}
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        pass
    return None


def cli_name(info: dict) -> str:
    argv = info.get("argv") or []
    if not argv:
        return ""
    first = Path(argv[0]).name.lower()
    if profile_for(first) == first:
        return first
    if (first in ("node", "bun", "sh", "bash") or first.startswith("python")) and len(argv) > 1:
        script = Path(argv[1]).stem
        if profile_for(script) == script:
            return script
        # npm/shell wrappers can exec a generic index.js instead of the CLI name.
        target = Path(argv[1]).resolve()
        profiles = json.loads((ROOT / "cli-profiles.json").read_text(encoding="utf-8"))
        for name in profiles:
            executable = shutil.which(name) if not name.startswith("_") else None
            if executable and Path(executable).resolve() == target:
                return name
    return ""


def enrolled_state(configured_role: str, env: dict | None = None) -> Path | None:
    """A nested or later CLI must not inherit its parent's pairing membership."""
    env = os.environ if env is None else env
    try:
        owner = int(env.get(PREFIX + "PID", "0"))
        if owner <= 0:
            return None
        expected = env.get(PREFIX + "PROFILE", "")
        root = process_info(owner)
        if not root or root["start"] != env.get(PREFIX + "START"):
            return None
        pid = os.getpid()
        for _ in range(32):
            info = process_info(pid)
            if not info:
                return None
            name = cli_name(info)
            if name or pid == owner:
                # npm's launcher may directly own the native CLI executable.
                wrapper = Path((root.get("argv") or [""])[0]).name.lower()
                direct_wrapper = (info["ppid"] == owner and
                                  Path(info["argv"][0]).name.lower() == expected and
                                  wrapper in ("node", "bun", "sh", "bash"))
                if pid != owner and (name != expected or not direct_wrapper):
                    return None
                if profile_for(configured_role) != expected:
                    raise ValueError("paired CLI profile does not match this MCP configuration")
                state = Path(env["PAIR_STATE"]).resolve()
                if not (state / "state.json").is_file():
                    raise ValueError("paired session state is missing; restart with pairup")
                return state
            if pid == owner or info["ppid"] <= 1:
                return None
            pid = info["ppid"]
    except (KeyError, TypeError):
        return None
    except ValueError as exc:
        if "paired" in str(exc):
            raise
    return None


def launch_argv(role: str, state: Path, command: list[str]) -> list[str]:
    return [sys.executable, str(Path(__file__).resolve()), "run", "--role", role,
            "--state", str(state), "--", *command]


def launch_shell(role: str, state: Path, command: str) -> str:
    return " ".join(shlex.quote(arg) for arg in launch_argv(role, state, shlex.split(command)))


def configured_role(base: str) -> str:
    role = os.environ.get("PAIR_ROLE", base)
    if (role != base and not role.startswith((base + "-", base + "_"))) or not re.fullmatch(r"[A-Za-z0-9_-]+", role):
        raise ValueError("paired role must be the configured role or a safe derived instance")
    return role


def ensure_daemon(role: str, state: Path) -> None:
    """Only an enrolled socket client may start its detached role daemon."""
    path = str(state / f".server-{role}.sock")
    def record_transport():
        if json.loads((state / "state.json").read_text(encoding="utf-8")).get("transport") != "socket":
            from store_core import Store
            Store(state).mutate(lambda data: data.update(transport="socket"))
    def ready():
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.2)
                probe.connect(path)
            return True
        except OSError:
            return False
    if ready():
        record_transport()
        return
    with (state / f"daemon-{role}.log").open("a") as log:
        process = subprocess.Popen([sys.executable, str(ROOT / "mcp/pair_mailbox/server.py"),
            "--role", role, "--state", str(state), "--socket", path],
            stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if ready():
            record_transport()
            return
        if process.poll() is not None:
            # Another enrolled connector may have won the same role lock.
            if ready():
                record_transport()
                return
            raise ValueError(f"paired daemon failed; see {state / ('daemon-' + role + '.log')}")
        time.sleep(0.05)
    raise ValueError(f"paired daemon did not become ready: {path}")


def run_cli(role: str, state: str, command: list[str]) -> int:
    profile = profile_for(role)
    info = process_info(os.getpid())
    if not profile or not info:
        raise ValueError("cannot establish paired CLI process identity")
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise ValueError("paired launch needs a CLI command")
    env = dict(os.environ)
    for key in list(env):
        if key.startswith(PREFIX) or key in ("PAIR_TRUST_ENV", "PAIR_STATE_DIR", "PAIR_SOCKET_PATH"):
            env.pop(key, None)
    env.update({PREFIX + "PID": str(os.getpid()), PREFIX + "START": info["start"],
                PREFIX + "PROFILE": profile, PREFIX + "ENGINE": str(ROOT),
                "PAIR_STATE": str(Path(state).resolve()), "PAIR_ROLE": role})
    profiles = json.loads((ROOT / "cli-profiles.json").read_text(encoding="utf-8"))
    prompt_arg = profiles[profile].get("initial_prompt")
    if prompt_arg:
        state_data = json.loads((Path(state) / "state.json").read_text(encoding="utf-8"))
        standing = "writer" if state_data.get("writer") == role else "read-only critic"
        prompt = (f"You were explicitly launched as {role}, initially the {standing}. "
                  "If MCP fails, retain that write boundary and repair the connection. Read pair_state, "
                  "then pair_task(action='list'), respect your assigned write boundary, "
                  "and work on the open task. "
                  "Communicate directly through pair_*; do not ask the user to relay messages. "
                  "If no task is open, report ready and wait.")
        command += ["--" if prompt_arg == "positional" else prompt_arg, prompt]
    os.execvpe(command[0], command, env)
    return 0


def standalone(stdin=None, stdout=None) -> int:
    """Complete MCP startup without registering a role or touching project state."""
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    for raw in stdin:
        try:
            request = json.loads(raw)
            if not isinstance(request, dict) or "id" not in request:
                continue
            method = request.get("method")
            if method == "initialize":
                params = request.get("params") or {}
                version = params.get("protocolVersion", "2024-11-05")
                result = {"protocolVersion": version,
                          "capabilities": {"tools": {"listChanged": False}},
                          "serverInfo": {"name": "pair-mailbox", "version": "standalone"},
                          "instructions": "Standalone CLI: perform the user's task normally, including native "
                          "subagents. No pair role is assigned. Do not call pair_* or wait for a peer. "
                          "Use pairup only when the user explicitly requests paired work."}
            elif method == "tools/list":
                result = {"tools": []}
            elif method == "resources/list":
                result = {"resources": []}
            elif method == "resources/templates/list":
                result = {"resourceTemplates": []}
            elif method == "prompts/list":
                result = {"prompts": []}
            elif method in ("ping", "logging/setLevel"):
                result = {}
            else:
                stdout.write(json.dumps({"jsonrpc": "2.0", "id": request["id"],
                    "error": {"code": -32601, "message": "No paired session; use pairup to join one."}}) + "\n")
                stdout.flush()
                continue
            stdout.write(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}) + "\n")
            stdout.flush()
        except (ValueError, TypeError):
            continue
    return 0


def main(argv: list[str]) -> int:
    if argv and argv[0] == "command":
        role, state = argv[1:3]
        profiles = json.loads((ROOT / "cli-profiles.json").read_text(encoding="utf-8"))
        command = profiles.get(profile_for(role), {}).get("launch")
        if not command:
            raise ValueError(f"no CLI launch profile for {role!r}")
        print(launch_shell(role, Path(state), command))
        return 0
    if argv and argv[0] == "run":
        parser = argparse.ArgumentParser()
        parser.add_argument("--role", required=True)
        parser.add_argument("--state", required=True)
        parser.add_argument("command", nargs=argparse.REMAINDER)
        args = parser.parse_args(argv[1:])
        return run_cli(args.role, args.state, args.command)
    if argv and argv[0] == "probe":
        state = enrolled_state(argv[1])
        if state is not None:
            print(state)
        return 0
    return standalone()


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except (ValueError, OSError) as error:
        print(f"pair-mailbox: {error}", file=sys.stderr)
        sys.exit(2)
