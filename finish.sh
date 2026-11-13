#!/usr/bin/env python3
"""Verify and install the engine, then use the normal explicit pair launcher.

Run from the engine clone. Use pairup directly for daily project work.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def run(argv, **kwargs):
    print("$ " + " ".join(str(arg) for arg in argv), flush=True)
    kwargs.setdefault("cwd", str(ROOT))
    return subprocess.run([str(arg) for arg in argv], **kwargs)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--task", default="", help="task for the pair")
    parser.add_argument("--cli", default="opencode", help="CLI for both panes with --duo")
    parser.add_argument("--duo", action="store_true", help="two copies of one CLI")
    parser.add_argument("--no-prune", action="store_true", help="keep dead roster entries")
    parser.add_argument("--no-pair", action="store_true", help="verify and install only")
    parser.add_argument("--state", default=os.environ.get("PAIR_STATE", ".pair"))
    parser.add_argument("--autonomy", default="", choices=("", "observe", "semi", "auto"))
    parser.add_argument("--roles", default="", help="preferred writer; legacy writer,critic accepts the writer")
    args = parser.parse_args(argv)
    if args.cli != "opencode" and not args.duo:
        parser.error("--cli requires --duo; use --roles for the writer of different CLIs")

    gate = run([ROOT / "scripts/verify-project.sh", "--jobs", "1"])
    if gate.returncode:
        print("Verification failed; installation and launch were not started.")
        return gate.returncode
    installed = run([sys.executable, ROOT / "install.sh"])
    if installed.returncode:
        return installed.returncode

    state = (ROOT / args.state).resolve()
    ctl = [ROOT / "bin/pairctl", "--state", state]
    writer = args.roles.split(",", 1)[0].strip()
    if writer:
        result = run([*ctl, "roles", "--writer", writer])
        if result.returncode:
            return result.returncode
    if args.autonomy:
        result = run([*ctl, "autonomy", args.autonomy])
        if result.returncode:
            return result.returncode
    if args.no_pair:
        print("Verified and installed; no model sessions launched.")
        return 0
    if not args.no_prune:
        result = run([*ctl, "roster", "--prune"])
        if result.returncode:
            return result.returncode

    task = args.task
    if args.autonomy:
        task = "autonomy: " + args.autonomy + ("\n" + task if task else "")
    if args.duo:
        command = [ROOT / "bin/pair-duo", "--state", state, args.cli]
        if task:
            command.append(task)
    else:
        command = [ROOT / "bin/pairup", "--state", state]
        if task:
            command.extend(["--task", task])
    # These launchers own enrollment, tmux isolation and the one supervisor.
    return run(command, cwd=str(state.parent)).returncode


if __name__ == "__main__":
    sys.exit(main())
