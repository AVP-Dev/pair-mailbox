"""The human authority over the round: who writes, which mode, checkpoint, task ledger.

Split out of `server.py` as its own responsibility: everything here CHANGES pair
state, and nothing here reads for show. The neighbours invert it: `pairctl_read.py`
only reads, `pairctl_panes.py` works the pane bindings.

Why a separate file, not "one more function in `server.py`": write authority is
exactly the boundary the whole protocol exists for. The code that moves it must
read whole in one pass, without wading through the MCP layer where `pair_state`
and `pair_send` live. Lying next to them, "who writes now" answers only after
reading the whole file.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import shutil
import sys

from server import (
    AUTONOMIES,
    MODES,
    OK_PHASES,
    OPEN_TASK,
    PairSession,
    ProtocolError,
    Store,
    autonomy_of,
    global_roles_path,
    next_task_id,
    open_tasks,
    permissions_path,
    render_tasks_md,
    resolve_autonomy,
    utcnow,
    write_tasks_md,
)


def _last_permit(store: Store) -> dict:
    """The last self-confirmation from the journal (P2, round 75).

    The human sees not "there was a permission" but WHAT exactly confirmed:
    exact_command and argv of the last unblock/unblock_refused event. No journal
    or no permissions means an empty dict, not an error: missing permissions is
    a normal state, not breakage.
    """
    import json as _json
    path = store.root / "transcript.jsonl"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    for raw in reversed(lines[-200:]):
        try:
            entry = _json.loads(raw)
        except ValueError:
            continue
        if entry.get("kind") in ("unblock", "unblock_refused"):
            return {
                "ts": entry.get("ts"),
                "verdict": "allow" if entry.get("kind") == "unblock" else "refuse",
                "exact_command": entry.get("exact_command", entry.get("prompt", "")),
                "argv": entry.get("argv", []),
                "why": entry.get("why", ""),
            }
    return {}


def cmd_status(store: Store, args: argparse.Namespace) -> int:
    state = store.read()
    session = PairSession("pairctl", store)
    # The operator saw raw state.policy (null) while the server refused on the
    # resolved _policy (project → global): "no pin", though one exists.
    effective = session._policy(state) or None
    raw = state.get("policy") if isinstance(state.get("policy"), dict) else None
    source = "project" if raw and raw.get("writer") else ("global" if effective else None)
    print(json.dumps(
        {
            "state_dir": str(store.root),
            "task": state.get("task", ""),
            "mode": state.get("mode", "fix"),
            "autonomy": autonomy_of(state),
            "writer": state.get("writer"),
            "policy": effective,
            "policy_source": source,
            "phase": state.get("phase"),
            "round": state.get("round"),
            "human_ok": session.human_ok_for_round(state),
            "evidence_this_round": len(session.round_evidence(state)),
            "unresolved_patches": [item["path"] for item in session.unresolved_patches(state)],
            "closing_blocked_by": session._done_blockers(state),
            "permission_allowlist": str(permissions_path(store.root)),
            "last_permit": _last_permit(store),
            "roles": {name: info.get("last_seen") for name, info in state.get("roles", {}).items()},
        },
        indent=2,
        ensure_ascii=False,
    ))
    return 0


def cmd_assign(store: Store, args: argparse.Namespace) -> int:
    role = args.role
    known = set(store.read().get("roles", {}))
    if known and role not in known:
        print(f"warning: {role!r} has never connected; registered roles: {', '.join(sorted(known))}")

    def apply(state):
        state["writer"] = role
        # the human decision merits a "chosen by human" mark, so the exchange
        # shows it is not a race
        state["writer_reason"] = "assigned by human"
        if state.get("closed_at") or state.get("phase") == "done":
            state.pop("closed_at", None)
            state["phase"] = "implement"
            state["round"] = int(state.get("round", 0)) + 1
        elif state.get("phase") == "scoping" and not state.get("task"):
            state["round"] = int(state.get("round", 0)) + 1
    store.mutate(apply)
    store.append_transcript("assign", "human", writer=role,
                            reason="assigned by human")
    print(f"write authority assigned to {role}")
    return 0


def cmd_roles(store: Store, args: argparse.Namespace) -> int:
    # Standing policy: ONLY the writer pins (writer-pin).
    # The critic is any second live roster entry, its name never stored in policy:
    # limits run out, CLIs change, and rewriting the policy on every critic swap
    # would halt work for paperwork. The --critic flag is accepted and ignored
    # (a warning, not an error): old calls and scripts must not fall after the
    # writer-pin move.
    # The race stays as fallback, but by default the writer is known upfront, and
    # the round starts with work, not rights trading.
    policy_path = global_roles_path()
    state = store.read()
    current = state.get("policy") if isinstance(state.get("policy"), dict) else {}
    wanted = dict(current)

    if args.clear:
        wanted = {}
    else:
        if args.writer:
            wanted = {"writer": args.writer}
        if args.critic:
            # Accepted and ignored: writer-pin stores no critic.
            print(f"warning: --critic {args.critic!r} ignored — "
                  f"the critic is now any second live one (writer-pin)", file=sys.stderr)
        if args.swap:
            # Swap with no stored critic means moving the writer to the second live one
            # from the roster; with no second live one — a refusal with a reason.
            roles = state.get("roles") or {}
            others = [r for r in roles if r != wanted.get("writer")]
            if not others:
                print("error: --swap has nobody: the roster holds no second live one", file=sys.stderr)
                return 2
            wanted = {"writer": others[0]}
            print(f"writer -> {others[0]} (first second-live from the roster)")

    if not args.clear and not args.writer and not args.swap:
        # A bare call and --critic without --writer mean read, not wipe: the critic
        # is never stored in writer-pin, and its "setup" would quietly drop policy
        # through the not wanted branch below. Show project + effective.
        session = PairSession("pairctl", store)
        effective = session._policy(state) or None
        print(json.dumps({"project": current or None, "effective": effective},
                         indent=2, ensure_ascii=False))
        return 0

    if not wanted:
        store.mutate(lambda st: st.pop("policy", None))
        store.append_transcript("roles", "human", policy=None)
        print("standing policy dropped, roles fall back to the race")
        return 0

    store.mutate(lambda st: st.__setitem__("policy", wanted))
    store.append_transcript("roles", "human", policy=wanted, source=str(policy_path))

    # The policy applies to the current round at once by naming the holder.
    holder = state.get("writer")
    if wanted.get("writer") and holder != wanted["writer"]:
        def apply(st):
            st["writer"] = wanted["writer"]
            st["writer_reason"] = "assigned by human standing policy"
        store.mutate(apply)

    if args.global_:
        policy_path.parent.mkdir(parents=True, exist_ok=True)
        policy_path.write_text(json.dumps(wanted, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"saved for all projects: {policy_path}")
    print(f"policy: writer {wanted.get('writer') or '—'} (critic — any second live one)")
    return 0


def cmd_release(store: Store, args: argparse.Namespace) -> int:
    def apply(state):
        state["writer"] = None
        state["writer_reason"] = ""
    store.mutate(apply)
    store.append_transcript("release", "human")
    print("write authority cleared; nobody holds it")
    return 0


def cmd_mode(store: Store, args: argparse.Namespace) -> int:
    mode = args.mode
    if mode not in MODES:
        print(f"error: unknown mode {mode!r}; expected one of {', '.join(MODES)}", file=sys.stderr)
        return 2
    previous = store.read().get("mode", "fix")
    store.mutate(lambda state: state.__setitem__("mode", mode))
    store.append_transcript("mode", "human", mode=mode, previous=previous)
    print(f"round mode is now {mode!r}")
    return 0


def cmd_autonomy(store: Store, args: argparse.Namespace) -> int:
    level = args.level
    if level not in AUTONOMIES:
        print(f"error: unknown autonomy {level!r}; expected one of {', '.join(AUTONOMIES)}",
              file=sys.stderr)
        return 2
    previous = autonomy_of(store.read())
    store.mutate(lambda state: state.__setitem__("autonomy", level))
    store.append_transcript("autonomy", "human", level=level, previous=previous)
    notes = {
        "observe": "the supervisor only reports: never restarts the fallen, never confirms permissions",
        "semi": 'the work runs itself, but the round never closes without evidence and without \'pairctl ok\'',
        "auto": "the work runs itself till the round close, only a recorded fact is needed",
    }
    print(f"autonomy is now {level!r}: {notes[level]}")
    return 0


def cmd_launch_autonomy(store: Store, args: argparse.Namespace) -> int:
    # Mode choice at start, exactly once and before the agents get instructions.
    # The logic lives here, not in pair-duo, because the bash wrapper has no tests:
    # a rule with nothing to check it by, we already found once.
    level, source = resolve_autonomy(
        store.root, task=args.task or "", flag=args.flag,
    )
    previous = autonomy_of(store.read())
    store.mutate(lambda state: state.__setitem__("autonomy", level))
    store.append_transcript(
        "autonomy", "launcher", autonomy=level, previous=previous, via=source,
    )
    print(f"autonomy {level!r} (via {source}): "
          f"{'pair works to the end' if level == 'auto' else level}")
    return 0


def cmd_ok(store: Store, args: argparse.Namespace) -> int:
    # One human word at the checkpoint. Exactly what makes semi "semi-auto": the
    # pair works alone but cannot close the round without it.
    #
    # --revoke takes the word back. Revocation is not for comfort: the human looks
    # at the edits, says "ok", and sees something else change after. Without revoke
    # the only exit is switching the setting or reset.
    state = store.read()
    round_number = int(state.get("round", 0))
    revoke = bool(getattr(args, "revoke", False))

    def apply(st):
        entries = st.setdefault("human_ok", [])
        same_round = [e for e in entries if int(e.get("round", -1)) == round_number]
        if same_round and bool(same_round[-1].get("revoke")) == revoke:
            return False
        entries.append({"round": round_number, "at": utcnow(), "phase": st.get("phase"),
                        "revoke": revoke})
        del entries[:-20]
        return True

    fresh = store.mutate(apply)
    store.append_transcript("human_ok_revoke" if revoke else "human_ok", "human",
                            round=round_number, phase=state.get("phase"), changed=fresh)
    if not fresh:
        print(f"round {round_number}: {'already revoked' if revoke else 'already marked'}; "
              f"nothing changed")
    elif revoke:
        print(f"ok revoked for round {round_number}; it cannot close until you say ok again")
    else:
        print(f"ok: round {round_number} marked at phase {state.get('phase')!r}")
        if state.get("phase") not in OK_PHASES:
            print(f"  note: ok counts only at {', '.join(OK_PHASES)}; "
                  f"this one will not close the round from {state.get('phase')!r}")
    return 0


def _wake_writer(store: Store, reason: str, text: str) -> None:
    # A human task is an event, not a record: without a wake-up the writer learns
    # it only on the next random read. Wake the record holder; with none (release)
    # there is nobody to wake — the human names one itself.
    writer = store.read().get("writer")
    if not writer:
        return
    try:
        from wake import record_wake
        record_wake(store.root, store.read(), writer, reason, text)
        store.append_transcript("wake_pending", "human", to=writer, reason=reason)
    except Exception:  # noqa: BLE001 - the wake file must not drop the command:
        # the task is already recorded above via mutate, wake best-effort, as in server.py
        pass


def cmd_task(store: Store, args: argparse.Namespace) -> int:
    # A list, not one line. A new task appends: the old never vanishes, and the round
    # never closes while something stays neither done nor dropped with a reason.
    # a reason.
    if args.done or args.drop:
        target = int(args.done or args.drop)
        want = "done" if args.done else "dropped"

        def close(st):
            match = next((i for i in st.get("tasks") or []
                          if int(i.get("id", 0)) == target), None)
            if not match:
                raise ProtocolError(f"no task #{target}")
            if want == "dropped" and not str(args.reason or "").strip():
                raise ProtocolError("--drop needs --reason")
            match["status"] = want
            match["reason"] = str(args.reason or "").strip()
            match["closed_at"] = utcnow()
            match["closed_by"] = "human"
            return match

        item = store.mutate(close)
        store.append_transcript("task_closed", "human", task_id=item["id"], status=want)
        print(f"task #{item['id']} is {want}")
        _wake_writer(store, "task", f"[pair] human: task #{item['id']} {want}, read pair_state")
    else:
        body = " ".join(str(args.text or "").split())
        if not body:
            print("task needs text", file=sys.stderr)
            return 2

        def apply(state):
            item = {"id": next_task_id(state), "text": body, "status": OPEN_TASK,
                    "added_at": utcnow(), "added_by": "human",
                    "round": int(state.get("round", 0)), "reason": "",
                    "closed_at": None, "closed_by": None}
            state.setdefault("tasks", []).append(item)
            state["task"] = body  # for old readers and the report
            return item

        item = store.mutate(apply)
        store.append_transcript("task_added", "human", task_id=item["id"], text=body[:200])
        print(f"task #{item['id']} added: {body}")
        _wake_writer(store, "task", f"[pair] human: new task #{item['id']}, read pair_state")
        still = [i["id"] for i in open_tasks(store.read())]
        if len(still) > 1:
            print(f"  open tasks now: {', '.join(str(i) for i in still)}")
    return 0


def cmd_tasks(store: Store, args: argparse.Namespace) -> int:
    current = store.read()
    path = write_tasks_md(store.root, current)
    print(f"# Session journal -> {path}\n")
    print(render_tasks_md(current))
    return 0


def cmd_reset(store: Store, args: argparse.Namespace) -> int:
    if not args.yes:
        print("refusing to reset without --yes", file=sys.stderr)
        return 2

    # \u00a711 calls the transcript the ground a run restores from: append-only
    # covers the file, and silent deletion wipes the audit. So an archive
    # (transcript-<date>.jsonl), not unlink; the wipe counts with the counters read,
    # so the human sees the destruction scale.
    archive_dir = store.root / "transcript-archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    old_lines = 0
    if store.transcript_path.exists():
        old_lines = sum(1 for _ in store.transcript_path.open(encoding="utf-8"))
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        store.transcript_path.rename(archive_dir / f"transcript-{stamp}.jsonl")
    for path in (store.mailbox_root, store.notes_root, store.patches_root):
        shutil.rmtree(path, ignore_errors=True)
    store.state_path.unlink(missing_ok=True)
    fresh = Store(store.root)
    # Wiping state must not raise rights — but rights count against the default,
    # and the default is now auto: exactly what the human set as norm. Setting
    # observe here would contradict: wipe would stop the pair against "work alone,
    # return with output" and force the human to notice. \u00a712 stays off-axis, so
    # auto never means "destructive allowed".
    # destructive allowed".
    fresh.mutate(lambda st: st.__setitem__("autonomy", "auto"))
    store.append_transcript("reset", "human", autonomy="auto")
    print("state reset; autonomy is now 'auto' — the pair works to the end and "
          "comes back with the result. Narrow it with `pairctl autonomy semi` "
          "or `pairctl autonomy observe`.")
    print(f"archived {old_lines} transcript lines; mode returns to 'fix' — "
          "audit guard is off until `pairctl mode audit` again.")
    return 0

def cmd_keys(store: Store, args: argparse.Namespace) -> int:
    """Generate the role Ed25519 pair: `pairctl keys gen <role>`."""
    if getattr(args, "keys_command", "") != "gen" or not getattr(args, "role", ""):
        print("usage: pairctl keys gen <role>", file=sys.stderr)
        return 2
    import base64
    import rolekeys
    role = args.role
    pub = rolekeys.keys_dir(store.root) / f"{role}.pub"
    old = base64.b64encode(pub.read_bytes()).decode("ascii") if pub.exists() else ""
    made = rolekeys.generate(store.root, store.project_root, role)

    def apply(state):
        entry = state.setdefault("role_keys", {}).setdefault(role, {"revoked": []})
        if old and old not in entry["revoked"]:
            entry["revoked"].append(old)
        entry.update({"key_id": made["key_id"], "public": made["public"]})

    store.mutate(apply)
    store.append_transcript("keys_gen", "human", target_role=role, key_id=made["key_id"], rekey=bool(old))
    print(f"{role}: key_id {made['key_id']}" + (" (old key revoked)" if old else ""))
    return 0