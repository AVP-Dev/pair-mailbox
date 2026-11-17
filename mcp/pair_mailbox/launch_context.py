"""Opt in one CLI process to pairing; ordinary global MCP starts stay inert."""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from pi_cli_policy import safe_pi_command
from project_workspace import initial_prompt, launch_state

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
        if platform.system() == "Darwin":
            return _darwin_process_info(pid)
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


class _ProcBsdInfo(ctypes.Structure):
    """Public Darwin ``proc_bsdinfo`` layout from sys/proc_info.h."""

    _fields_ = [
        ("pbi_flags", ctypes.c_uint32), ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32), ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32), ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32), ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32), ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32), ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * 16), ("pbi_name", ctypes.c_char * 32),
        ("pbi_nfiles", ctypes.c_uint32), ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32), ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32), ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    ]


def _darwin_sysctl_args(pid: int) -> list[str] | None:
    """Read argv through KERN_PROCARGS2 without executing SIP-protected /bin/ps."""
    libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    sysctl = libc.sysctl
    sysctl.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_uint, ctypes.c_void_p,
                       ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
    sysctl.restype = ctypes.c_int
    mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2, pid
    length = ctypes.c_size_t()
    if sysctl(mib, 3, None, ctypes.byref(length), None, 0) != 0:
        return None
    if length.value < struct.calcsize("=i") or length.value > 2 * 1024 * 1024:
        return None
    buffer = ctypes.create_string_buffer(length.value)
    if sysctl(mib, 3, buffer, ctypes.byref(length), None, 0) != 0:
        return None
    raw = buffer.raw[:length.value]
    argc = struct.unpack_from("=i", raw)[0]
    if argc <= 0 or argc > 4096:
        return None
    # KERN_PROCARGS2 stores the executable path first, followed by NUL padding,
    # then exactly argc NUL-terminated argv strings.
    cursor = struct.calcsize("=i")
    path_end = raw.find(b"\0", cursor)
    if path_end < 0:
        return None
    cursor = path_end + 1
    while cursor < len(raw) and raw[cursor] == 0:
        cursor += 1
    argv = []
    for _ in range(argc):
        end = raw.find(b"\0", cursor)
        if end < 0:
            return None
        argv.append(os.fsdecode(raw[cursor:end]))
        cursor = end + 1
    return argv


def _darwin_process_info(pid: int) -> dict | None:
    """Use libproc + KERN_PROCARGS2, both available inside Seatbelt sandboxes."""
    try:
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        proc_pidinfo = libproc.proc_pidinfo
        proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                                 ctypes.POINTER(_ProcBsdInfo), ctypes.c_int]
        proc_pidinfo.restype = ctypes.c_int
        info = _ProcBsdInfo()
        received = proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
        if received != ctypes.sizeof(info) or info.pbi_pid != pid:
            return None
        argv = _darwin_sysctl_args(pid)
        if argv is None:
            return None
        start = f"{info.pbi_start_tvsec}.{info.pbi_start_tvusec:06d}"
        return {"pid": pid, "ppid": int(info.pbi_ppid), "start": start, "argv": argv}
    except (AttributeError, OSError, ValueError, struct.error):
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
                                  wrapper in ("node", "bun", "sh", "bash", "sandbox-exec"))
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


def role_socket_path(state: Path, role: str) -> str:
    """Stable IPC path inside a private state dir or a private short socket dir."""
    direct = state.resolve() / f".server-{role}.sock"
    direct_is_private = False
    if len(os.fsencode(direct)) <= 88:
        try:
            info = direct.parent.stat()
            direct_is_private = info.st_uid == os.getuid() and not info.st_mode & 0o077
        except OSError:
            pass
    if direct_is_private:
        return str(direct)
    digest = hashlib.sha256(f"{state.resolve()}\0{role}".encode()).hexdigest()[:32]
    return str(Path("/tmp") / f"pair-mailbox-{os.getuid()}" / f"{digest}.sock")


def prepare_role_socket_path(state: Path, role: str) -> str:
    """Create/check a user-private short socket directory before daemon startup."""
    path = Path(role_socket_path(state, role))
    if path.parent == Path("/tmp"):
        return str(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.parent.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError(f"refusing insecure pair socket directory: {path.parent}")
    return str(path)


UNSAFE_FLAGS = {
    "--dangerously-skip-permissions",
    "--allow-dangerously-skip-permissions",
    "--dangerously-bypass-approvals-and-sandbox",
    "--dangerously-bypass-hook-trust",
    "--yolo",
    "-y",
    "--auto-approve",
    "--no-sandbox",
    "--disable-sandbox",
    "--sandbox=disabled",
}


def _codex_config_key(value: str) -> str:
    if "=" not in value:
        raise ValueError("Codex config override needs a KEY=VALUE")
    return value.split("=", 1)[0].strip().lower()


def _reject_codex_security_override(value: str) -> None:
    key = _codex_config_key(value)
    protected = {"sandbox_mode", "approval_policy", "approvals_reviewer", "auto_review"}
    if key in protected or key == "sandbox_workspace_write" or key.startswith("sandbox_workspace_write."):
        raise ValueError("Codex launch overrides may not replace sandbox/approval config")


def safe_cli_command(profile: str, role: str, is_writer: bool,
                     command: list[str]) -> list[str]:
    """Enforce safe per-role CLI flags; OS sandbox and protocol enforce file authority."""
    if not command:
        raise ValueError("paired launch needs a CLI command")
    executable = Path(command[0]).name.lower()
    if executable != profile:
        raise ValueError(f"paired launch for {role!r} must execute {profile!r}, not {executable!r}")
    if any(arg in UNSAFE_FLAGS or arg.startswith("--dangerously-") for arg in command[1:]
           if arg.startswith("-")):
        raise ValueError(f"refusing unsafe permission/sandbox bypass in {profile} launch")
    if profile == "omp" and any(arg == "--approval-mode=yolo" for arg in command[1:]):
        raise ValueError("OMP yolo approval mode is forbidden")

    args = command[1:]
    if profile == "codex":
        # --approve-for-me itself selects auto-review + workspace-write; Codex
        # intentionally makes it conflict with explicit --sandbox/-s flags.
        # Remove overrideable permission switches and append the role policy last.
        cleaned: list[str] = []
        index = 0
        while index < len(args):
            arg = args[index]
            if arg in ("--sandbox", "-s", "--ask-for-approval", "-a"):
                if index + 1 >= len(args):
                    raise ValueError(f"Codex option {arg} needs a value")
                index += 2
                continue
            if arg in ("--cd", "-C", "--add-dir"):
                if index + 1 >= len(args):
                    raise ValueError(f"Codex option {arg} needs a value")
                index += 2
                continue
            if arg == "--worktree":
                index += 1
                continue
            if arg.startswith(("--sandbox=", "--ask-for-approval=", "--cd=", "--add-dir=")):
                index += 1
                continue
            if arg.startswith("-C") and arg != "-C":
                index += 1
                continue
            if arg == "--not-so-yolo":
                index += 1
                continue
            if arg == "--approve-for-me":
                index += 1
                continue
            if arg == "--full-auto":
                index += 1
                continue
            setting = ""
            if arg in ("-c", "--config"):
                if index + 1 >= len(args):
                    raise ValueError("Codex config override needs a KEY=VALUE")
                _reject_codex_security_override(args[index + 1])
                cleaned.extend((arg, args[index + 1]))
                index += 2
                continue
            if arg.startswith("--config="):
                _reject_codex_security_override(arg.split("=", 1)[1])
                cleaned.append(arg)
                index += 1
                continue
            if arg.startswith("-c") and arg != "-C":
                _reject_codex_security_override(arg[2:])
            cleaned.append(arg)
            index += 1
        args = cleaned
        if is_writer:
            # An untrusted CODEX_HOME/config.toml must not widen the auto-reviewer's
            # writable roots or turn network on inside workspace-write.
            args += ["-c", "sandbox_workspace_write.writable_roots=[]",
                     "-c", "sandbox_workspace_write.network_access=false",
                     "--approve-for-me"]
        else:
            args += ["--sandbox", "read-only", "--ask-for-approval", "never"]
    elif profile == "claude":
        cleaned = []
        index = 0
        while index < len(args):
            arg = args[index]
            if arg == "--permission-mode":
                if index + 1 >= len(args):
                    raise ValueError("Claude --permission-mode needs a value")
                if args[index + 1] == "bypassPermissions":
                    raise ValueError("Claude bypassPermissions mode is forbidden")
                index += 2
                continue
            if arg.startswith("--permission-mode="):
                if arg.split("=", 1)[1] == "bypassPermissions":
                    raise ValueError("Claude bypassPermissions mode is forbidden")
                index += 1
                continue
            if arg in ("--add-dir", "--cwd"):
                if index + 1 >= len(args):
                    raise ValueError(f"Claude option {arg} needs a value")
                index += 2
                continue
            if arg in ("--settings", "--setting-sources", "--permission-prompts",
                       "--permission-prompt-tool") or arg.startswith(("--settings=", "--setting-sources=",
                                                                      "--permission-prompts=",
                                                                      "--permission-prompt-tool=")):
                raise ValueError("Claude launch overrides may not replace managed sandbox settings")
            cleaned.append(arg)
            index += 1
        safe_settings = json.dumps({
            "sandbox": {
                "enabled": True,
                "failIfUnavailable": True,
                "allowUnsandboxedCommands": False,
                "autoAllowBashIfSandboxed": True,
                "excludedCommands": [],
                "enableWeakerNestedSandbox": False,
                "enableWeakerNetworkIsolation": False,
            }
        }, separators=(",", ":"))
        args = cleaned + ["--settings", safe_settings,
                          "--permission-mode", "auto" if is_writer else "dontAsk",
                          "--permission-prompts", "none"]
    elif profile == "agy":
        cleaned = []
        index = 0
        while index < len(args):
            arg = args[index]
            if arg == "--mode":
                if index + 1 >= len(args):
                    raise ValueError("agy --mode needs a value")
                if args[index + 1] in ("always-proceed", "yolo", "bypassPermissions"):
                    raise ValueError(f"agy {args[index + 1]!r} permission mode is forbidden")
                index += 2
                continue
            if arg.startswith("--mode="):
                value = arg.split("=", 1)[1]
                if value in ("always-proceed", "yolo", "bypassPermissions"):
                    raise ValueError(f"agy {value!r} permission mode is forbidden")
                index += 1
                continue
            if arg in ("--add-dir", "--log-file"):
                if index + 1 >= len(args):
                    raise ValueError(f"agy option {arg} needs a value")
                index += 2
                continue
            if arg.startswith("--add-dir="):
                index += 1
                continue
            if arg == "--sandbox" or arg.startswith("--sandbox="):
                index += 1
                continue
            cleaned.append(arg)
            index += 1
        args = cleaned + ["--mode=auto", "--sandbox"]
    elif profile == "opencode":
        args = [arg for arg in args if arg not in ("--auto", "--auto=false")]
        # Keep auto approval before the positional prompt. OpenCode treats
        # trailing arguments as prompt text, so appending after the prompt
        # silently changes the task instead of enabling autonomous mode.
        insertion = 1 if args and not args[0].startswith("-") else 0
        args.insert(insertion, "--auto")
    elif profile == "pi":
        return safe_pi_command(command, is_writer)
    elif profile == "omp":
        cleaned = []
        index = 0
        while index < len(args):
            arg = args[index]
            if arg in ("--approval-mode", "--add-dir", "--cwd"):
                if index + 1 >= len(args):
                    raise ValueError(f"OMP option {arg} needs a value")
                if arg == "--approval-mode" and args[index + 1] == "yolo":
                    raise ValueError("OMP yolo approval mode is forbidden")
                index += 2
                continue
            if arg.startswith("--approval-mode="):
                if arg.split("=", 1)[1] == "yolo":
                    raise ValueError("OMP yolo approval mode is forbidden")
                index += 1
                continue
            if arg.startswith(("--add-dir=", "--cwd=")):
                index += 1
                continue
            if arg == "--config" or arg.startswith("--config="):
                raise ValueError("OMP config overlays may not replace managed approval settings")
            cleaned.append(arg)
            index += 1
        args = cleaned + ["--approval-mode=write"]

    return [command[0], *args]


def _protected_project_paths(state: Path, workspace: Path) -> list[Path]:
    """Include both checked-out trees and shared Git metadata in the deny set."""
    roots = {workspace.resolve(), state.resolve().parent}
    try:
        state_data = json.loads((state / "state.json").read_text(encoding="utf-8"))
        engine = state_data.get("engine_root")
        if engine and Path(engine).exists():
            roots.add(Path(engine).resolve())
    except (OSError, ValueError):
        pass
    for root in tuple(roots):
        if not root.is_dir():
            continue
        for option in ("--git-dir", "--git-common-dir"):
            try:
                result = subprocess.run(
                    ["git", "rev-parse", "--path-format=absolute", option], cwd=root,
                    capture_output=True, text=True, timeout=5)
            except (OSError, subprocess.SubprocessError):
                continue
            if result.returncode == 0 and result.stdout.strip():
                roots.add(Path(result.stdout.strip()).resolve())
        try:
            worktrees = subprocess.run(
                ["git", "worktree", "list", "--porcelain"], cwd=root,
                capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            continue
        if worktrees.returncode == 0:
            roots.update(Path(line[9:]).resolve() for line in worktrees.stdout.splitlines()
                         if line.startswith("worktree "))
    return sorted(roots, key=lambda item: str(item))


def _break_external_hardlinks(protected: list[Path], workspace: Path) -> None:
    """Detach protected regular files from aliases writable outside the tree.

    Seatbelt checks a pathname, while hard links name the same inode. The
    Critic's path-based write deny is therefore safe only when existing links
    are detached and its sandbox also denies creating new links.
    """
    try:
        device = workspace.stat().st_dev
    except OSError as exc:
        raise ValueError(f"critic launch refused: cannot inspect workspace: {exc}") from exc

    roots: list[Path] = []
    for path in sorted(protected, key=lambda item: (len(item.parts), str(item))):
        try:
            info = path.stat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ValueError(f"critic launch refused: cannot inspect protected root {path}: {exc}") from exc
        if info.st_dev != device or not path.is_dir():
            continue
        if any(path == root or root in path.parents for root in roots):
            continue
        roots.append(path)

    pending = list(roots)
    while pending:
        directory = pending.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError as exc:
            raise ValueError(f"critic launch refused: cannot scan protected directory {directory}: {exc}") from exc
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if entry.stat(follow_symlinks=False).st_dev == device:
                        pending.append(Path(entry.path))
                    continue
                info = entry.stat(follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode) or info.st_dev != device or info.st_nlink <= 1:
                    continue
                if info.st_uid != os.getuid():
                    raise ValueError(
                        f"critic launch refused: linked protected file is not owned by this user: {entry.path}")
                fd, temporary_name = tempfile.mkstemp(prefix=".pair-unalias-", dir=directory)
                os.close(fd)
                temporary = Path(temporary_name)
                try:
                    shutil.copy2(entry.path, temporary, follow_symlinks=False)
                    current = os.stat(entry.path, follow_symlinks=False)
                    if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                        raise OSError("protected file changed during hard-link isolation")
                    os.replace(temporary, entry.path)
                finally:
                    try:
                        temporary.unlink()
                    except FileNotFoundError:
                        pass
            except ValueError:
                raise
            except OSError as exc:
                raise ValueError(
                    f"critic launch refused: cannot isolate linked protected file {entry.path}: {exc}") from exc


def role_sandbox_argv(role: str, state: Path, command: list[str],
                      workspace: Path | None = None,
                      system: str | None = None) -> list[str]:
    """Place a non-writer behind an OS-enforced project and Git read-only rule."""
    state_data = json.loads((state / "state.json").read_text(encoding="utf-8"))
    if state_data.get("writer") == role:
        return command
    system = platform.system() if system is None else system
    workspace = (workspace or Path.cwd()).resolve()
    protected = _protected_project_paths(state, workspace)
    if system in ("Darwin", "Linux"):
        _break_external_hardlinks(protected, workspace)
    socket_parent = Path(role_socket_path(state, role)).parent.resolve()
    if socket_parent not in protected and not any(socket_parent == root or root in socket_parent.parents
                                                   for root in protected):
        protected.append(socket_parent)
    if system == "Darwin":
        sandbox_exec = shutil.which("sandbox-exec")
        if not sandbox_exec:
            raise ValueError("critic launch refused: macOS sandbox-exec is unavailable")
        clauses = " ".join(
            f'(deny file-write* (subpath "{str(path).replace(chr(92), chr(92) * 2).replace(chr(34), chr(92) + chr(34))}"))'
            for path in protected
        )
        # `file-link` is the SBPL operation for link(2); `file-write-link` is
        # not a valid Seatbelt operation on current macOS. Existing aliases
        # were detached above before this policy is applied.
        profile = f"(version 1) (allow default) (allow process-exec*) (deny file-link) {clauses}"
        wrapped = [sys.executable, str(Path(__file__).resolve()), "exec-child",
                   "--role", role, "--state", str(state.resolve()), "--profile",
                   profile_for(role), "--", *command]
        return [sandbox_exec, "-p", profile, *wrapped]
    if system == "Linux":
        bwrap = shutil.which("bwrap")
        if not bwrap:
            raise ValueError("critic launch refused: install bubblewrap for the Linux read-only boundary")
        from linux_sandbox import hardlink_filter_fd
        seccomp_fd = hardlink_filter_fd()
        wrapped = [sys.executable, str(Path(__file__).resolve()), "exec-child",
                   "--role", role, "--state", str(state.resolve()), "--profile",
                   profile_for(role), "--", *command]
        # CLI sessions stay writable; protected source roots and hardlink syscalls do not.
        argv = [bwrap, "--die-with-parent", "--seccomp", str(seccomp_fd), "--bind", "/", "/",
                "--dev-bind", "/dev", "/dev", "--proc", "/proc"]
        for path in protected:
            if path.is_dir():
                argv += ["--ro-bind", str(path), str(path)]
        return [*argv, "--", *wrapped]
    raise ValueError(f"critic launch refused: no OS read-only backend for {system}")


def exec_child(role: str, state: str, profile: str, command: list[str]) -> int:
    """Exec the CLI without changing the PID the MCP role binds to."""
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise ValueError("paired launch needs a CLI command")
    info = process_info(os.getpid())
    if not info:
        raise ValueError("cannot establish sandboxed CLI process identity")
    env = dict(os.environ)
    for key in list(env):
        if key.startswith(PREFIX) or key in ("PAIR_TRUST_ENV", "PAIR_STATE_DIR", "PAIR_SOCKET_PATH"):
            env.pop(key, None)
    env.update({PREFIX + "PID": str(os.getpid()), PREFIX + "START": info["start"],
                PREFIX + "PROFILE": profile, PREFIX + "ENGINE": str(ROOT),
                "PAIR_STATE": str(Path(state).resolve()), "PAIR_ROLE": role,
                "PAIR_LAUNCH_PROFILE": profile})
    os.execvpe(command[0], command, env)
    return 0


def configured_role(base: str) -> str:
    role = os.environ.get("PAIR_ROLE", base)
    if (role != base and not role.startswith((base + "-", base + "_"))) or not re.fullmatch(r"[A-Za-z0-9_-]+", role):
        raise ValueError("paired role must be the configured role or a safe derived instance")
    return role


def ensure_daemon(role: str, state: Path) -> None:
    """Only an enrolled socket client may start its detached role daemon."""
    path = prepare_role_socket_path(state, role)
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
    state_path = Path(state).resolve()
    state_data = launch_state(state_path, role, Path.cwd())
    is_writer = state_data.get("writer") == role
    command = safe_cli_command(profile, role, is_writer, command)
    env = dict(os.environ)
    for key in list(env):
        if key.startswith(PREFIX) or key in ("PAIR_TRUST_ENV", "PAIR_STATE_DIR", "PAIR_SOCKET_PATH"):
            env.pop(key, None)
    env.update({PREFIX + "PID": str(os.getpid()), PREFIX + "START": info["start"],
                PREFIX + "PROFILE": profile, PREFIX + "ENGINE": str(ROOT),
                "PAIR_STATE": str(state_path), "PAIR_ROLE": role,
                "PAIR_LAUNCH_PROFILE": profile,
                "PAIR_SOCKET_PATH": role_socket_path(state_path, role)})
    if not is_writer or state_data.get("transport") == "socket":
        # The role daemon stays outside the critic's sandbox. MCP writes are
        # therefore mediated by protocol methods, not direct filesystem access.
        ensure_daemon(role, state_path)
    profiles = json.loads((ROOT / "cli-profiles.json").read_text(encoding="utf-8"))
    prompt_arg = profiles[profile].get("initial_prompt")
    if prompt_arg:
        state_data = json.loads((Path(state) / "state.json").read_text(encoding="utf-8"))
        standing = "writer" if state_data.get("writer") == role else "read-only critic"
        autonomy = state_data.get("autonomy", "auto")
        resume_task = None
        resume_task_id = os.environ.get("PAIR_RESUME_TASK_ID", "").strip()
        if resume_task_id:
            from store_core import Store
            selected_state = Store(state_path).read()
            resume_task = next((item for item in selected_state.get("tasks", [])
                                if str(item.get("id")) == resume_task_id), None)
            if resume_task is None:
                raise ValueError(f"selected resume task #{resume_task_id} disappeared before launch")
        resume_workspace = os.environ.get("PAIR_RESUME_WORKSPACE", "").strip()
        prompt = initial_prompt(role, standing, autonomy, resume_task, resume_workspace)
        env.pop("PAIR_RESUME_TASK_ID", None)
        env.pop("PAIR_RESUME_WORKSPACE", None)
        command += ["--" if prompt_arg == "positional" else prompt_arg, prompt]
    command = role_sandbox_argv(role, state_path, command, workspace=Path.cwd())
    if not is_writer:
        # role_sandbox_argv's exec-child rebinds the process identity to the
        # post-sandbox CLI PID so MCP membership survives the OS wrapper.
        os.execvpe(command[0], command, env)
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
    if argv and argv[0] == "exec-child":
        parser = argparse.ArgumentParser()
        parser.add_argument("--role", required=True)
        parser.add_argument("--state", required=True)
        parser.add_argument("--profile", required=True)
        parser.add_argument("command", nargs=argparse.REMAINDER)
        args = parser.parse_args(argv[1:])
        return exec_child(args.role, args.state, args.profile, args.command)
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
