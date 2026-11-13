#!/usr/bin/env python3
"""Thin MCP client over the pair daemon's unix socket (round 106 slice F).

What it replaces: the CLI's MCP entry spawns `serve.sh <role>` AS A CHILD, so
the server's lifetime equals the CLI's. Measured cost: a dropped MCP meant
relaunching the CLI (losing its session context) or driving the CLI's own /mcp
picker (measured: four failure shapes — invisible focus, Escape aborting the
handshake, a stale modal eating commands).

What this does: connect to the daemon the supervisor already keeps alive and
pump the same newline-delimited JSON-RPC 2.0 between the CLI's stdio and the
socket. The client is a pipe, nothing more — it holds no protocol state, so a
CLI restart reconnects (the refresh) and the daemon's role lock, notes and
journal survive untouched.

Usage (from an MCP config's command array):
    python3 mcp_socket_client.py <role> [--state DIR] [--socket PATH]

Exit paths, deliberate:
- daemon missing -> EXIT 4 with one line telling pairup to raise it, because the
  client's job is to connect, not to spawn: a client that silently spawned would
  rebuild the child-process lifetime this slice exists to remove.
- stdin closed (the CLI is gone or restarting) -> exit 0. The CLI's death is not
  an error of the transport; the daemon keeps serving the next connector.
"""
from __future__ import annotations

import json
import os
import queue
import socket
import sys
import threading
import time
from pathlib import Path


def _resolve_state_dir(argv: list[str]) -> Path:
    for i, arg in enumerate(argv):
        if arg == "--state" and i + 1 < len(argv):
            return Path(argv[i + 1])
        if arg.startswith("--state="):
            return Path(arg[len("--state="):])
    env = (os.environ.get("PAIR_STATE") or "").strip()
    if env:
        return Path(env)
    return Path.cwd() / ".pair"


def _resolve_socket(state_dir: Path, argv: list[str]) -> str:
    for i, arg in enumerate(argv):
        if arg == "--socket" and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith("--socket="):
            return arg[len("--socket="):]
    env = (os.environ.get("PAIR_SOCKET_PATH") or "").strip()
    if env:
        return env
    return str(state_dir / ".server-pair.sock")


def _connect(path: str, deadline: float = 10.0) -> socket.socket:
    """Connect, retrying while the daemon is still coming up.

    A daemon raised moments ago needs a moment to bind; refusing instantly would
    make the FIRST reconnect of a fresh boot look like a dead daemon. Ten seconds
    is the honest bound, not forever.
    """
    end = time.time() + deadline
    last: OSError | None = None
    while time.time() < end:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(path)
            sock.settimeout(None)
            return sock
        except OSError as exc:
            last = exc
            sock.close()
            if isinstance(exc, FileNotFoundError):
                time.sleep(0.2)
                continue
            time.sleep(0.2)
    raise (last or OSError(f"cannot connect to {path}"))


def _redial(path: str, deadline: float):
    """Re-connect to the daemon within a bounded window (round 113 slice 1).

    Why: the limitation measured live on the flipped pair — the client dialled
    ONCE, a dying daemon left it reporting `pair Failed` for the rest of the
    session, and reconnect granularity was the SESSION rather than the event.

    Why bounded and not a supervisor: the client must never spawn a daemon, never
    manage a role, never outlive its CLI. It dials, and if nothing answers inside
    the window it reports the same failure it reports today — with the same
    remedy. The window is seconds, not forever: an operator needs to know.
    """
    end = time.time() + deadline
    while time.time() < end:
        time.sleep(0.2)
        try:
            fresh = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            fresh.settimeout(2.0)
            fresh.connect(path)
            fresh.settimeout(None)
            return fresh
        except OSError:
            continue
    return None


def _redial_seconds() -> float:
    """PAIR_SOCKET_REDIAL seconds, 0 by default (today's behaviour)."""
    raw = (os.environ.get("PAIR_SOCKET_REDIAL") or "0").strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 0.0


def pump(sock: socket.socket, stdin, stdout, path: str) -> int:
    """Forward frames both ways; JSON-RPC notifications do not have replies."""
    writer = sock.makefile("w", encoding="utf-8", newline="\n")
    output_lock = threading.Lock()
    eof = object()
    no_event = object()

    def start_reader(connection: socket.socket):
        responses: queue.Queue = queue.Queue()
        disconnected = threading.Event()

        def read_frames() -> None:
            reader = connection.makefile("r", encoding="utf-8", newline="\n")
            try:
                for raw in reader:
                    try:
                        message = json.loads(raw)
                    except json.JSONDecodeError:
                        message = None
                    if isinstance(message, dict) and "id" in message:
                        responses.put(message["id"])
                    with output_lock:
                        stdout.write(raw if raw.endswith("\n") else raw + "\n")
                        stdout.flush()
            except (OSError, ValueError):
                pass
            finally:
                reader.close()
                disconnected.set()
                responses.put(eof)

        thread = threading.Thread(target=read_frames, daemon=True)
        thread.start()
        return responses, disconnected, thread

    responses, disconnected, reader_thread = start_reader(sock)

    def reconnect(replay: str | None) -> bool:
        nonlocal sock, writer, responses, disconnected, reader_thread
        window = _redial_seconds()
        if window <= 0:
            print("pair client: daemon closed the connection", file=sys.stderr)
            return False
        print(f"pair client: daemon closed the connection; re-dialing for {window:g}s",
              file=sys.stderr)
        fresh = _redial(path, window)
        if fresh is None:
            print("pair client: no daemon answered inside the window", file=sys.stderr)
            return False
        try:
            writer.close()
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass
        sock = fresh
        writer = sock.makefile("w", encoding="utf-8", newline="\n")
        responses, disconnected, reader_thread = start_reader(sock)
        if replay is not None:
            try:
                writer.write(replay + "\n")
                writer.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                disconnected.set()
        return True

    try:
        while True:
            raw = stdin.readline()
            if not raw:
                break
            line = raw.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
                expects_reply = not isinstance(message, dict) or "id" in message
                request_id = message.get("id") if isinstance(message, dict) else None
            except json.JSONDecodeError:
                expects_reply = True
                request_id = None
            if disconnected.is_set() and not reconnect(None):
                return 3
            try:
                writer.write(line + "\n")
                writer.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                disconnected.set()
                if not reconnect(line):
                    return 3
            if not expects_reply:
                continue
            while True:
                try:
                    response_id = responses.get(timeout=0.1)
                except queue.Empty:
                    response_id = no_event
                if response_id == request_id:
                    break
                if response_id is eof:
                    if not reconnect(line):
                        return 3
                    continue
                if disconnected.is_set() and not reconnect(line):
                    return 3
    finally:
        try:
            writer.close()
        except OSError:
            pass
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass
        reader_thread.join(timeout=1)
    return 0


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0 if argv else 2
    from launch_context import configured_role, enrolled_state, ensure_daemon, standalone
    explicit = any(arg in ("--state", "--socket") or arg.startswith(("--state=", "--socket="))
                   for arg in argv[1:])
    enrolled = enrolled_state(argv[0]) if not explicit else None
    if not explicit and enrolled is None:
        return standalone()
    state_dir = enrolled or _resolve_state_dir(argv)
    path = _resolve_socket(state_dir, argv)
    if enrolled is not None:
        role = configured_role(argv[0])
        path = str(state_dir / f".server-{role}.sock")
        ensure_daemon(role, state_dir)
    if not os.path.exists(path):
        print(f"pair client: no daemon at {path} — pairup raises it "
              f"(pairrestart --role <role> refreshes it)", file=sys.stderr)
        return 4
    try:
        sock = _connect(path)
    except OSError as exc:
        # A socket FILE that refuses every connection is a dead daemon that left
        # it behind — the same fact as "no daemon", and the same remedy. Saying
        # only "cannot connect" made a dead daemon look like a network problem
        # (measured: the client printed Errno 111 while the remedy was obvious).
        try:
            os.unlink(path)
        except OSError:
            pass
        print(f"pair client: no daemon at {path} (the socket file refused "
              f"every connection: {exc}) — pairup raises it, "
              f"pairrestart --role <role> refreshes it", file=sys.stderr)
        return 4
    return pump(sock, sys.stdin, sys.stdout, path)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
