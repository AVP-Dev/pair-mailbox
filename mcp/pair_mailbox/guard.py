"""One rule must never take the whole pass down with it.

The measured death (2026-10-25, this project's own supervisor): one
`tmux send-keys` into a pane whose CLI had stopped consuming input hung for
the full 5-second timeout and raised `subprocess.TimeoutExpired`. The pilot's
`flush_outbox` caught `ComposerBusy` and nothing else, so the exception
escaped the tick, the whole `pairpilot` died, and EVERY rule it carried went
dark with it — the silence check, the stalled-agent check, and the monologue
watchdog. A dead supervisor is the one failure no watchdog can notice.

So the guard lives here, as its own unit, and the supervisor calls it:

    - `guarded` wraps one call that must not escape.
    - `run_rules` runs a list of rules, each in its own guard, so a rule that
      throws is recorded and skipped while the others still run.

The supervisor's value is the SUM of its rules. One failure in one of them
must not silently switch the rest off, and it must not be silent either:
every failure is journalled, so it is visible instead of merely absent.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Iterable

Rule = Callable[[dict], object]
Log = Callable[[str], None]
Journal = Callable[..., None]


def _journal(journal: Journal | None, kind: str, **fields: object) -> None:
    """Write to the journal, or lose the record — never lose the pass.

    The journal is a side effect of the guard, not the guard itself: if the
    journal is broken too, the rule failure still must not escape. So this
    helper swallows its own errors.
    """
    if journal is None:
        return
    try:
        journal(kind, **fields)
    except Exception:  # noqa: BLE001 - the journal is best-effort too
        pass


def guarded(call: Callable[[], object], log: Log,
            journal: Journal | None = None,
            kind: str = "pilot_call_failed",
            **fields: object) -> object:
    """Run `call`, and turn any exception into a record instead of a death.

    Returns the call's value, or None when it raised.
    """
    try:
        return call()
    except Exception as exc:  # noqa: BLE001 - the supervisor must survive
        log(f"{kind}: {exc}")
        _journal(journal, kind, error=str(exc)[:200], **fields)
        return None


def run_rules(rules: Iterable[Rule], state: dict, log: Log,
              journal: Journal | None = None) -> list[str]:
    """Run every rule, each inside its own guard.

    Returns the names of the rules that failed, so the caller can report
    them. A failed rule never stops the ones after it: the pass continues.
    """
    failed: list[str] = []
    for rule in rules:
        name = getattr(rule, "__name__", repr(rule))
        if name == "<lambda>":
            name = getattr(rule, "__qualname__", name)
        try:
            rule(state)
        except Exception as exc:  # noqa: BLE001 - one rule, not the pass
            failed.append(name)
            log(f"rule {name} failed: {exc}")
            _journal(journal, "pilot_rule_failed", rule=name, error=str(exc)[:200])
    return failed


# The rules of a supervisor pass, in order, BY NAME. The caller binds them to
# its own methods — the supervisor owns the rules, this module owns the
# ORDER and the reason for it, so the order is one fact in one place.
#
# Polling runs BEFORE raising new wake-ups and before printing: whoever
# already woke has its wake-up dropped silently, and nothing lands in the
# print queue. If polling ran after flush_outbox, the tick where the role
# just woke would still print into its composer.
#
# The sweeper runs LAST (opencode seq 1505): before the work it could delete
# a stamp a check in this same tick was about to consult — a duplicate nudge
# in the same tick, the exact spam stamps prevent.
RULE_ORDER: tuple[str, ...] = (
    "ambiguous", "gone_roles", "unblock", "restart_agent", "dead_pane",
    "poll_wakes", "unread_peers", "pending_answers", "stalled_patches",
    "check_stalled_busy", "check_monologue", "check_blocked_composer",
    "stale_writer", "idle_roles",
    "drop_stale_outbox", "flush_outbox", "preventive_compact",
    "check_server_freshness", "checkpoint", "report_once", "sweep_stamps",
)


def bind_rules(owner: object, order: Iterable[str] = RULE_ORDER,
               log: Log | None = None) -> list[Rule]:
    """Bind the named rules to `owner`'s methods, in order.

    `sweep_stamps` takes no state, so it is wrapped to the same shape as
    every other rule — one call, one state.
    """
    bound: list[Rule] = []
    for name in order:
        if name == "sweep_stamps":
            bound.append(lambda state, _m=getattr(owner, name): _m())
        elif name == "check_blocked_composer":
            bound.append(lambda state, _o=owner: blocked_composer_rule(_o, state, log))
        else:
            bound.append(getattr(owner, name))
    return bound


def blocked_composer_rule(owner: object, state: dict, log: Log) -> None:
    """Say who is blocked by a composer that holds somebody ELSE's text.

    The measured silence (2026-10-26, this project's own round 146): the
    critic's pane sat frozen for 2.8 hours with the human's unsent line in
    its composer — typed by hand and never submitted. Every delivery was
    correctly refused (the text is not ours, so it stays untouched), every
    wake was raised, every escalation was queued... and the one person who
    could unblock it never learned that it was blocked.

    The refusal was right. The silence was the defect: the supervisor watches
    the ROOM, and a room where one role cannot be reached is a fact about the
    room. So the pilot says it — on the console, where the human looks, and
    in the journal, so the state is provable. It never types into the composer
    and never erases the text: that stays the human's, and this rule only
    REPORTS it.

    Once per role per round: a blocked composer is a standing fact, and a
    fact repeated every tick is noise.
    """
    from composer import capture, composer_line, is_composer_empty, pane_alive
    tmux = getattr(owner, "tmux", None)
    if tmux is None:
        return
    skip = getattr(owner, "skip", set())
    remember = getattr(owner, "remember", None)
    if remember is None:
        return
    for role, info in (state.get("roles") or {}).items():
        if role in skip or not isinstance(info, dict):
            continue
        pane = info.get("pane")
        if not pane or not pane_alive(tmux, pane):
            continue
        try:
            line = composer_line(capture(tmux, pane))
        except Exception:  # noqa: BLE001 - the pane may vanish between checks
            continue
        if line is None or is_composer_empty(line):
            continue
        key = f"blocked-composer-{role}-{state.get('round')}"
        if not remember(key, ttl=600.0):
            continue
        text = line.strip()[-80:]
        log(f"{role}: its composer holds text that is not ours — delivery to it "
            f"is refused until it is sent or cleared (composer: {text})")
        owner.store.append_transcript("pilot_composer_blocked", "pairpilot",
                                      target=role, pane=pane, composer=text)


def write_attributed_note(store: object, notes_dir: Path, kind: str, body: str,
                           role: str = "pairpilot") -> str:
    """Append a note the way the server writes it — with sha and attempt.

    The measured block (2026-10-26, this project's own round 146): the
    monologue cut wrote `notes/fix.md` by hand — a timestamp and a role, and
    nothing else. The notes-consistency gate reads the note's own sha and
    attempt and matches them against the journal's `note` rows; a note with
    no sha cannot be attributed, so the gate called it forged and BLOCKED the
    round close. The supervisor's own record of its work was the thing that
    stopped the round from shipping.

    So the body is normalized, hashed, the attempt is counted from the
    journal, and a `note` row lands carrying the sha and the attempt — one
    writer of that shape, so the gate's rule and the writer cannot drift.

    Returns the sha it wrote, or "" when the body was empty.
    """
    import hashlib
    body = (body or "").strip()
    if not body:
        return ""
    normalized = "\n".join(line.rstrip() for line in body.splitlines()).strip()
    body_sha = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
    attempt = 1
    for row in store.read_transcript(limit=50):
        if row.get("kind") != "note" or row.get("note_kind") != kind:
            continue
        if row.get("role") == role and str(row.get("note_sha", "")) == body_sha:
            attempt = int(row.get("note_attempt", 0)) + 1
    notes_dir.mkdir(parents=True, exist_ok=True)
    target = notes_dir / f"{kind}.md"
    stamp = _utcnow()
    with store.lock():
        with target.open("a", encoding="utf-8") as stream:
            stream.write(f"\n## {stamp} — {role} "
                         f"(note {body_sha} attempt {attempt})\n\n{normalized}\n")
    store.append_transcript("note", role, note_kind=kind, chars=len(body),
                            note_sha=body_sha, note_attempt=attempt)
    return body_sha


def _utcnow() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
