"""`pairctl`: the human CLI over pair state. Entry point and argument parsing.

Split out of `server.py` as its own responsibility: only command grammar and
routing live here, while the commands themselves live in three neighbouring
modules by state ownership:

- `pairctl_authority.py` — changes the round: who writes, mode, autonomy,
  checkpoint, task ledger, reset;
- `pairctl_panes.py` — tmux pane addresses;
- `pairctl_read.py` — reading only: digest, mailbox, journal, notes, project
  overview (`bin/pairchat` imports the last from there).

Why not one 725-line `pairctl_cli.py` module as `server.py` was: packing was
moving the monolith, not splitting on boundaries. Boundaries here follow state
ownership, and show what lands in the journal: a `pairctl_read.py` command
cannot break anything, because no state write lives in it at all.

`server.py` stays the MCP server entry point, and `--state`/`--role` parse in
it too: the file name and path must never move, `serve.sh`, `install.sh` and
the MCP config of all four roles depend on them.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Callable

from server import (
    AUTONOMIES,
    DIGEST_TOKEN_BUDGET,
    LOG_DEFAULT_LIMIT,
    MODES,
    Store,
)

# Neighbour-module imports stay lazy, and that is not optimization. The `pairctl_*`
# modules import `server`, so top level would close a cycle: `server` calls
# `main`, and `main` calls here. Inside a function no cycle forms, because by
# then `server` already loads.
Command = Callable[[Store, argparse.Namespace], int]


def _commands() -> dict[str, str]:
    """Command name -> implementing module.

    A name dict, not imports: the import itself happens in `run_ctl`. So every
    command stays in its file, and this one knows nothing of their contents
    beyond the module name existing.
    """
    return {
        "status": "pairctl_authority",
        "assign": "pairctl_authority",
        "roles": "pairctl_authority",
        "release": "pairctl_authority",
        "read-ack": "pairctl_authority",
        "mode": "pairctl_authority",
        "autonomy": "pairctl_authority",
        "launch-autonomy": "pairctl_authority",
        "ok": "pairctl_authority",
        "task": "pairctl_authority",
        "tasks": "pairctl_authority",
        "reset": "pairctl_authority",
        "keys": "pairctl_authority",
        "pane": "pairctl_panes",
        "roster": "pairctl_panes",
        "panes": "pairctl_panes",
        "digest": "pairctl_read",
        "why": "pairctl_read",
        "inbox": "pairctl_read",
        "log": "pairctl_read",
        "all": "pairctl_read",
        "notes": "pairctl_read",
        "budget": "budget",
        "worktree": "worktree",
        "pr": "review_gate",
    }

def run_ctl(args: argparse.Namespace, state_dir: Path) -> int:
    """Parse the command and call its implementation.

    The order is deliberate: `Store` builds BEFORE the unknown-command refusal.
    An unknown command used to build a store too, but invisibly — no state was
    written. The check now runs first, so a typo in the command name never reads
    as "the server did something".
    """
    command = args.ctl_command
    module_name = _commands().get(command or "")
    if not module_name:
        print(f"unknown command {command!r}", file=sys.stderr)
        return 2
    module = __import__(module_name)
    handler: Command = getattr(module, "cmd_" + command.replace("-", "_"))
    # Round 115 S4: `Store` is built LAZILY, and only the commands that touch a
    # state directory pay for it. `cmd_all` deliberately ignores the store —
    # "all" walks several projects on purpose (pairctl_read.py:101-103) — so it
    # must not need one, and a checkout with no pair at all (CI) must still run
    # `pairctl all`. Measured: with a Store built for every command, CI failed
    # `pairctl-read` on `FileNotFoundError: No state.json in .../.pair`, because
    # `walk_up_to_state` falls back to `cwd/.pair` and CI's cwd has no pair.
    # `task` writes the first state of a fresh project, so it is the one command
    # allowed to create; the rest read a pair that must already exist, and a
    # typo'd `--state` stays a loud refusal — the whole point of the gate.
    class _LazyStore:
        """The Store, built on first use. A command that never touches it (all,
        and any future cross-project one) never triggers the refusal."""

        def __init__(self, root: Path, allow_create: bool) -> None:
            self._root = root
            self._allow = allow_create
            self._store: Store | None = None

        def __getattr__(self, name: str) -> Any:
            if self._store is None:
                self._store = Store(self._root, allow_create=self._allow)
            return getattr(self._store, name)

    return handler(_LazyStore(state_dir, command == "task"), args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pair-mailbox",
        description="MCP server and control tool for a two-agent collaboration.",
    )
    parser.add_argument("--role", help="This agent's role name. Fixed at configuration time.")

    sub = parser.add_subparsers(dest="ctl_command")
    sub.add_parser("status", help="show shared state")

    read_ack = sub.add_parser(
        "read-ack",
        help="human: acknowledge or skip a role's pending read batch",
        description="A batch `pair_read` returned but whose ack never arrived stays "
                    "pending forever and blocks the round from closing. After three "
                    "replays it is escalated; the human then either ACKs it by token "
                    "or skips it with a reason.",
    )
    read_ack.add_argument("role", help="whose pending batch")
    read_ack.add_argument("token", help="the batch_id from pair_state.pending_read")
    read_ack.add_argument("--skip", action="store_true",
                          help="drop the batch instead of advancing to it; needs --reason "
                               "and only works after replay escalation")
    read_ack.add_argument("--reason", default="",
                          help="why the batch is skipped; required with --skip")

    assign = sub.add_parser("assign", help="human: give write authority to a role")
    assign.add_argument("role")

    sub.add_parser("release", help="human: clear write authority")

    digest = sub.add_parser(
        "digest",
        help="human: the same bounded slice the agents get, in under 500 tokens",
    )
    digest.add_argument("--budget", type=int, default=DIGEST_TOKEN_BUDGET,
                        help=f"token ceiling for the whole answer, {DIGEST_TOKEN_BUDGET} by default")
    digest.add_argument("--role", default="",
                        help="say 'you are the writer' when this is this role, saves a lookup")

    pane = sub.add_parser(
        "pane",
        help="human: bind a role to a pane by hand, or --clear to go back to what tmux says",
    )
    pane.add_argument("--role", required=False, default="",
                      help="role whose binding to set (or positional: pane <role> [address])")
    pane.add_argument("positional", nargs="*",
                      help="positional shape from the card: <role> [address]; "
                           "address: pane number or <session>:<window>.<pane>; "
                           "no address — show only, never record")
    pane.add_argument("--current", action="store_true",
                      help="bind the role to the current pane $TMUX_PANE; without the flag "
                           "pane <role> changes nothing, only shows")
    pane.add_argument("--clear", action="store_true",
                      help="drop the override; the pane goes back to what the process reports")

    roster = sub.add_parser("roster", help="human: show who is registered, --prune to clean")
    roster.add_argument("--prune", action="store_true",
                        help="remove roles whose process is dead AND whose pane is gone")

    sub.add_parser("panes", help="human: live tmux panes mapped to project, role and liveness")

    roles = sub.add_parser(
        "roles",
        help="human: pin who always writes and who always reviews, instead of racing each round",
    )
    roles.add_argument("--writer", help="role that always holds write authority")
    roles.add_argument("--critic", help="role that always reviews")
    roles.add_argument("--swap", action="store_true", help="exchange writer and critic")
    roles.add_argument("--clear", action="store_true", help="drop the pinning, go back to the race")
    roles.add_argument("--global", dest="global_", action="store_true",
                       help="also remember it for every project (PAIR_ROLES, default ~/.config/pair-mailbox/roles.json)")

    round_mode = sub.add_parser(
        "mode",
        help="human: choose what a round is for — 'fix' changes code, 'audit' does not",
    )
    round_mode.add_argument("mode", choices=list(MODES))

    autonomy = sub.add_parser(
        "autonomy",
        help="human: how far the pair may go without you — 'observe', 'semi' or 'auto'",
    )
    autonomy.add_argument("level", choices=list(AUTONOMIES))

    launch = sub.add_parser(
        "launch-autonomy",
        help="choose the level once at launch, from a flag, a task line, .pair/autonomy, "
             "or the default 'auto' — strongest source first",
    )
    launch.add_argument("--flag", default=None, choices=list(AUTONOMIES),
                        help="what bin/pair-duo was given")
    launch.add_argument("--task", default="", help="the task text, may start with 'autonomy: semi'")

    ok = sub.add_parser(
        "ok",
        help="human: say yes at the verify checkpoint, so the pair may close the round",
    )
    ok.add_argument("--revoke", action="store_true",
                    help="human: take the yes back, e.g. after looking at what changed since")

    task = sub.add_parser("task", help="human: add a task to the shared list")
    task.add_argument("text", nargs="?")
    task.add_argument("--drop", metavar="ID", default=None,
                      help="close task ID as dropped, with the reason below")
    task.add_argument("--done", metavar="ID", default=None,
                      help="close task ID as done")
    task.add_argument("--reason", default="",
                      help="why a task is dropped; required, an empty reason is refused")

    sub.add_parser("tasks", help="human: print the shared task list with statuses")

    inbox = sub.add_parser("inbox", help="show a role's pending messages")
    inbox.add_argument("role")

    log = sub.add_parser("log", help="print the audit trail")
    log.add_argument("--limit", type=int, default=LOG_DEFAULT_LIMIT,
                     help=f"how many of the newest entries to print, {LOG_DEFAULT_LIMIT} by default")
    log.add_argument("--all", action="store_true",
                     help="print the whole active file; rotated-away entries live "
                          "in .pair/transcript-archive/ and are not included")

    sub.add_parser("notes", help="print shared notes")

    everywhere = sub.add_parser(
        "all", help="one row per project, across every project with a .pair"
    )
    everywhere.add_argument("paths", nargs="*", help="extra project or .pair directories")
    everywhere.add_argument("--stuck-min", type=int, default=30)
    everywhere.add_argument("--json", action="store_true")

    why = sub.add_parser("why", help="who writes, and the reason they gave")
    why.add_argument("paths", nargs="*", help="extra project or .pair directories")
    why.add_argument("--json", action="store_true")

    reset = sub.add_parser("reset", help="wipe shared state")
    reset.add_argument("--yes", action="store_true")

    keys = sub.add_parser("keys", help="human: role Ed25519 keys (gen)")
    keys.add_argument("keys_command", nargs="?", help="'gen'")
    keys.add_argument("role", nargs="?", default="", help="role for gen")

    budget_p = sub.add_parser("budget", help="human: round limits and metrics (budget)")
    budget_p.add_argument("--reset", action="store_true", help="reset the current round budget counters")
    budget_p.add_argument("--json", action="store_true", help="print metrics as json")

    wt_p = sub.add_parser("worktree", help="human: git worktree management for parallel sessions")
    wt_p.add_argument("worktree_action", nargs="?", choices=["list", "add", "remove", "prune"], default="list")
    wt_p.add_argument("branch", nargs="?", help="branch name (for add)")
    wt_p.add_argument("--target", help="path or branch for remove")
    wt_p.add_argument("--force", action="store_true", help="force removal (for remove)")
    wt_p.add_argument("--json", action="store_true", help="print as json")

    pr_p = sub.add_parser("pr", help="human/writer: PR merge machine gate (merge)")
    pr_p.add_argument("pr_action", nargs="?", choices=["merge"], default="merge")
    pr_p.add_argument("pr_id", nargs="?", help="PR number or branch name")
    pr_p.add_argument("--force", action="store_true", help="skip checks (needs --reason)")
    pr_p.add_argument("--reason", default="", help="forced-merge reason with --force")

    return parser


def ctl_main(argv: list[str], store: Store | None = None) -> int:
    """Run a `pairctl` command on a ready argv.

    `store` passes only in tests that edit state directly. Live runs never have
    it, and `Store` builds here: one creation point means "state dir chosen"
    happens in exactly one place.
    """
    from pairctl_read import extract_state

    state_dir, rest = extract_state(argv)
    args = build_parser().parse_args(rest)
    if not args.ctl_command:
        build_parser().print_help()
        return 0
    if store is not None:
        command = args.ctl_command
        module = __import__(_commands()[command])
        return getattr(module, "cmd_" + command.replace("-", "_"))(store, args)
    return run_ctl(args, state_dir)