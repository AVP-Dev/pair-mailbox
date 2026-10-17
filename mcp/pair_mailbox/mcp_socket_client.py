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
import socket
import sys
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


def pump(sock: socket.socket, stdin, stdout) -> int:
    """Frames in both directions until the CLI closes stdin.

    One line in, one line out: the daemon's framing IS the CLI's framing, so the
    client adds nothing to the protocol and can never desynchronise it.
    """
    reader = sock.makefile("r", encoding="utf-8", newline="\n")
    writer = sock.makefile("w", encoding="utf-8", newline="\n")
    try:
        while True:
            raw = stdin.readline()
            if not raw:
                break
            line = raw.strip()
            if not line:
                continue
            writer.write(line + "\n")
            writer.flush()
            reply = reader.readline()
            if not reply:
                # the daemon died under us: not a protocol error, a dead transport
                print("pair client: daemon closed the connection", file=sys.stderr)
                return 3
            stdout.write(reply if reply.endswith("\n") else reply + "\n")
            stdout.flush()
    finally:
        try:
            reader.close()
            writer.close()
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass
    return 0


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0 if argv else 2
    state_dir = _resolve_state_dir(argv)
    path = _resolve_socket(state_dir, argv)
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
    return pump(sock, sys.stdin, sys.stdout)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
