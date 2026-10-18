"""Binding roles to tmux panes: `pairctl pane` and `pairctl roster`.

Split out of `server.py` as its own responsibility. Both commands that read
and change the pane address live here, and both pass through the same
`pin_verdict` check. Keeping them together is not about line order but safety:
"this role sits here" must never part from "where to print at all" with one
of the two forgotten.

A special fail-closed note, and it belongs to this file. `pin_verdict` returns
three states: `ok`, `bad` (proven dead or foreign address) and `unknown` (nothing
to check with — tmux unreachable). The first and the last differ here, not in
the check itself, because the caller decides, and the two callers decide
differently:

- `pairctl pane` refuses on both `bad` and `unknown`: the human never chose the
  address yet, and an unverifiable one must not be recorded;
- `_register` in `server.py` drops the pin only on `bad`, and on `unknown`
  keeps it in force while never printing into that pane. A five-second tmux
  failure must not cancel the human decision forever.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

from server import Store, is_pid_alive, pane_exists, same_pane, utcnow

# Three states, not two, and the difference is not stylistic.
#
# `bad` means "proven dead or misdirected". The only state where the
# pin drops. `unknown` means "nothing to check with": tmux unreachable
# or silent. Both used to return one refused, while `_register` dropped the
# pin on ANY refusal — a five-second tmux failure wiped the human decision
# forever.
#
# Fail-closed holds where the decision is made: never print into a pane with
# nothing to check it by. But wiping the pin over a transient outage is banned
# too: "unconfirmed" and "proven unusable" are different things.
PIN_OK = "ok"
PIN_BAD = "bad"
PIN_UNKNOWN = "unknown"


def pin_verdict(tmux, pin: str, project_root: Path) -> tuple[str, str]:
    """Whether the pinned pane still holds, and if not — why.

    A human override ("this role sits here") must pass the same check as an
    automatically picked address. Otherwise `pairctl pane` would open a hole in
    the isolation it was built for: one wrong address, and the role mail leaves
    for another project — exactly what items 1 and 2 fixed.

    Two independent refusals, not interchangeable:
    - no pane (`pane_alive`) — the reference went stale;
    - foreign pane (`pane_is_foreign`) — the reference lives but points away.

    Both yield `PIN_BAD`. No tmux (or its failure on the pane) yields `PIN_UNKNOWN`:
    nothing to confirm by, but nothing to refute by either. The caller decides
    its own way: `pairctl pane` refuses (an unverifiable address must not be
    recorded), while `_register` keeps the pin in force and just never prints
    into that pane.
    """
    if not pin:
        return PIN_BAD, "address not set"
    if not tmux:
        return PIN_UNKNOWN, "tmux unreachable, nothing to check with"
    from composer import pane_alive, pane_is_foreign
    # Condition (c): composer.pane_alive/pane_is_foreign already resolve through
    # composer._tmux(), which reads the socket — so this module must HAND them a
    # socket-aware tmux, not the bare binary, or another project's pane would
    # read as ours (round 106 slice C). The import sits at function level to keep
    # the module's import graph as it was, but it must be REAL: a missing one
    # broke eight tests under verify-project while a direct pytest run passed
    # only because the name resolved from a partially imported server module.
    from server import tmux_command
    import shlex as _shlex
    tmux = " ".join(_shlex.split(tmux_command()))
    if not pane_alive(tmux, pin):
        return PIN_BAD, "no pane left at this address"
    try:
        foreign, why_foreign = pane_is_foreign(tmux, pin, project_root)
    except Exception as exc:  # noqa: BLE001 - a transient tmux failure is no verdict on the address
        return PIN_UNKNOWN, f"pane unreadable: {exc}"
    if foreign:
        return PIN_BAD, f"foreign pane: {why_foreign}"
    return PIN_OK, "pane alive and ours"


def cmd_pane(store: Store, args: argparse.Namespace) -> int:
    # A human-hand pane-binding override.
    #
    # Why it exists when the address self-resolves from the pane anyway: `_register`
    # takes the address from the process `TMUX_PANE` environment, and for a server
    # re-registering itself after a restart that environment runs foreign or stale. Editing
    # state.json by hand never helps here: `_register` rewrites `pane` on every call,
    # and the "fix" lives till the next ring. The `pane_pin` key reads before the
    # environment, so it wins.
    #
    # The address is measured by the same bar as the automatic one, otherwise the
    # override would open a hole in the cross-project isolation.
    tmux = shutil.which("tmux")
    # Two call shapes, one meaning (pairctl-pane-missing card):
    #   pairctl pane --role <role> [address] [--clear]   # original, stays
    #   pairctl pane <role> [address]                    # positional, from the card
    # No address means show only, never record: `pane <role>` from a live pane must
    # answer, not pin. Binding to the current pane happens only through the
    # explicit `--current` flag: otherwise consecutive polls of two roles from one
    # window quietly seat both on one pane, and the supervisor skips them as
    # indistinguishable (shared_panes -> skip). Cause measured: %2205 behind both
    # opencode and pi after `pane opencode` + `pane pi` from ml:0.0.
    positionals = list(getattr(args, "positional", None)
                       or ([getattr(args, "address", None)]
                           if getattr(args, "address", None) else []))
    role = getattr(args, "role", None) or ""
    address = None
    if role and positionals:
        address = positionals[0]
    elif not role and len(positionals) >= 2:
        role, address = positionals[0], positionals[1]
    elif not role and len(positionals) == 1:
        role = positionals[0]
    if not role:
        print("error: name a role: `pane <role> [address]` or `pane --role <role> [address]`",
              file=sys.stderr)
        return 2
    if not address and getattr(args, "current", False) and not args.clear:
        env_pane = (os.environ.get("TMUX_PANE") or "").strip()
        if env_pane:
            address = env_pane
    state = store.read()
    roles_now = state.setdefault("roles", {})
    if role not in roles_now:
        print(f"warning: {role!r} has never connected; registered roles: "
              f"{', '.join(sorted(roles_now)) or '—'}")

    if args.clear:
        if not roles_now.get(role, {}).get("pane_pin"):
            print(f"role {role} already has no pane override")
            return 0
        store.mutate(lambda st: (st["roles"][role].pop("pane_pin", None),
                                 st["roles"][role].pop("pane_pin_why", None)))
        store.append_transcript("pane_pin_cleared", role, by="human")
        print(f"the role pane override {role} dropped, back to tmux")
        return 0

    if not address:
        info = roles_now.get(role, {})
        if not info:
            print(f"role {role} never connected; pane unknown")
            return 1
        pin = info.get("pane_pin")
        if pin:
            print(f"{role} pinned to {pin}")
            print(f"  {info.get('pane_pin_why') or 'reason unrecorded'}")
            print(f"  recorded into the roster: {info.get('pane')}")
        else:
            print(f"{role} has no override, pane from tmux: "
                  f"{info.get('pane') or '—'}")
        return 0

    verdict, why = pin_verdict(tmux, address, store.project_root)
    if verdict != PIN_OK:
        # Refusal here is mandatory and identical for `bad` and `unknown`: recording a
        # bad address means the very next `pair_deliver` leaves for another project or
        # nowhere, and recording an unverifiable one means storing what we never knew.
        # The difference between them surfaces later, in `_register`: `bad` wipes the
        # pin, `unknown` keeps it in force.
        # `unknown` keeps it in force.
        print(f"error: {address!r} unfit: {why}", file=sys.stderr)
        return 1

    def apply_pin(st):
        entry = st.setdefault("roles", {}).setdefault(
            role, {"first_seen": utcnow(), "reads": 0, "sends": 0})
        previous = entry.get("pane")
        entry["pane_pin"] = address
        entry["pane_pin_why"] = f"pinned by human: {why}"
        # Write into `pane` at once, so the override takes effect before the next tool
        # call, not only after it.
        entry["pane"] = address
        entry["last_seen"] = entry.get("last_seen") or utcnow()
        return previous

    previous = store.mutate(apply_pin)
    store.append_transcript("pane_pin_set", role, by="human",
                            pane=address, was=previous, why=why)
    print(f"{role} pinned to {address} ({why})")
    if previous and previous != address:
        print(f"  was: {previous}")
    return 0


def cmd_panes(store: Store, args: argparse.Namespace) -> int:
    # Live tmux panes -> project (cwd) -> roster role -> pid liveness.
    # Read-only: never touches state, never writes the journal. Answers "which pane
    # is whose", once needing ps + tmux + state.json.
    import subprocess
    tmux = shutil.which("tmux")
    if not tmux:
        print("tmux unreachable, no panes to read")
        return 1
    try:
        out = subprocess.run(
            [tmux, "list-panes", "-a", "-F",
             "#{pane_id}\t#{session_name}:#{window_index}.#{pane_index}\t#{pane_current_path}\t#{pane_current_command}\t#{pane_pid}\t#{pane_title}"],
            capture_output=True, text=True, timeout=10)
    except Exception as exc:  # noqa: BLE001 - tmux fell over, say so instead of lying
        print(f"tmux silent: {exc}")
        return 1
    if out.returncode != 0:
        print(f"tmux refused: {(out.stderr or '').strip()[:120]}")
        return 1
    state = store.read()
    roles_now = state.get("roles") or {}
    # Pane -> roles of this project recorded on it.
    by_pane: dict[str, list[str]] = {}
    for role, info in roles_now.items():
        pane = info.get("pane")
        if pane:
            by_pane.setdefault(str(pane), []).append(role)
    lines = out.stdout.splitlines()
    if not lines:
        print("no live panes")
        return 0
    print(f"{'pane':14} {'whose':7} {'project (cwd)':36} {'role':12} {'process'}")
    for line in lines:
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        uid, addr, cwd, cmd, pid = parts[0], parts[1], parts[2], parts[3], parts[4]
        title = parts[5] if len(parts) > 5 else ""
        ours = ""
        try:
            ours = "OURS" if Path(cwd).resolve() == store.project_root.resolve() \
                or store.project_root.resolve() in Path(cwd).resolve().parents else "foreign"
        except OSError:
            ours = "?"
        claim = ",".join(sorted(by_pane.get(uid, []) + by_pane.get(addr, []))) or "—"
        if title and title not in ("", cmd):
            claim += f" [{title}]"
        alive = "alive" if pid.strip().isdigit() and is_pid_alive(int(pid.strip())) else "?"
        print(f"{addr:14} {ours:7} {cwd[-34:]:36} {claim:12} {alive} {cmd}")
    print(f"\n{len(lines)} panes; project: {store.project_root} (OURS — this project)")
    return 0


def cmd_roster(store: Store, args: argparse.Namespace) -> int:
    #
    # Bare `roster` only shows. `--prune` deletes, and deletes on two independent
    # facts at once: the process is dead AND the pane is gone. One fact never
    # suffices — a dead agent may leave a live pane behind, while a live pid on
    # another pane means the record points away.
    #
    # Silent deletes are banned: the roster is the pair shared memory, and the human
    # must see what was removed and why.
    current = store.read()
    roles_now = current.get("roles") or {}
    # Who is alive and on which pane. Needed to tell "no pane" from "a pane, but
    # not this one".
    living = [role for role, info in roles_now.items()
              if isinstance(info.get("pid"), int) and info["pid"] > 0
              and is_pid_alive(info["pid"])]
    entries = []
    for role, info in roles_now.items():
        pid = info.get("pid")
        pane = info.get("pane")
        pid_dead = isinstance(pid, int) and pid > 0 and not is_pid_alive(pid)
        pane_gone = bool(pane) and not pane_exists(pane)
        # Different formats of one pane must never read as different panes, otherwise
        # `--prune` would delete the record just because the neighbour is recorded by
        # session address and this one by number.
        holder = next((other for other in living
                       if other != role
                       and same_pane(roles_now[other].get("pane"), pane)), None)
        entries.append((role, info, pid_dead, pane_gone or holder is not None, holder))
    if not args.prune:
        if not entries:
            print("roster empty")
            return 0
        for role, info, pid_dead, wrong, holder in sorted(entries):
            mark = "alive" if not pid_dead else "dead"
            extra = ", no pane" if wrong and not holder else ""
            if holder:
                extra = f", pane held by live role {holder}"
            print(f"{role:18} pid={info.get('pid')} pane={info.get('pane') or '—'}  {mark}{extra}")
        print("\n--prune drops entries with a dead pid and no pane "
              "or a pane held by another live role")
        return 0

    doomed = {role: True for role, _, pid_dead, wrong, _ in entries
              if pid_dead and wrong}
    if not doomed:
        print("nothing to clean: no entries with a dead pid and no pane at once")
        return 0
    if current.get("writer") in doomed:
        print(f"refusing to prune {current['writer']!r}: it still holds write authority. "
              f"Move it with `pairctl assign` or `pairctl release` first.",
              file=sys.stderr)
        return 2

    def prune(state):
        roles = state.get("roles") or {}
        for role in doomed:
            roles.pop(role, None)
        state["roles"] = roles

    store.mutate(prune)
    store.append_transcript("roster_pruned", "human", removed=sorted(doomed))
    for role in sorted(doomed):
        print(f"dropped {role}")
    return 0