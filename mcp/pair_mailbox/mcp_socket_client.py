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
            try:
                writer.write(line + "\n")
                writer.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                # The daemon died under us and the socket says so at the WRITE,
                # which is the same fact as an empty reply — treat them alike.
                reply = ""
            else:
                reply = reader.readline()
            if not reply:
                # the daemon died under us: not a protocol error, a dead transport
                window = _redial_seconds()
                if window > 0:
                    print(f"pair client: daemon closed the connection; "
                          f"re-dialing for {window:g}s", file=sys.stderr)
                    fresh = _redial(path, window)
                    if fresh is not None:
                        try:
                            reader.close()
                            writer.close()
                            sock.close()
                        except OSError:
                            pass
                        sock = fresh
                        reader = sock.makefile("r", encoding="utf-8", newline="\n")
                        writer = sock.makefile("w", encoding="utf-8", newline="\n")
                        # The frame is NOT lost: it was in the lost writer, and the
                        # new daemon is waiting for it while we wait on stdin —
                        # a deadlock neither side can see. Re-send it, so exactly
                        # one frame is in flight after a re-dial, never zero.
                        try:
                            writer.write(line + "\n")
                            writer.flush()
                            reply = reader.readline()
                        except (BrokenPipeError, ConnectionResetError, OSError):
                            reply = ""
                        if reply:
                            stdout.write(reply if reply.endswith("\n") else reply + "\n")
                            stdout.flush()
                            continue
                        continue
                    print("pair client: no daemon answered inside the window",
                          file=sys.stderr)
                    return 3
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
    return pump(sock, sys.stdin, sys.stdout, path)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
