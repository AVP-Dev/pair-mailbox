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
    "check_stalled_busy", "check_monologue", "stale_writer", "idle_roles",
    "drop_stale_outbox", "flush_outbox", "preventive_compact",
    "check_server_freshness", "checkpoint", "report_once", "sweep_stamps",
)


def bind_rules(owner: object, order: Iterable[str] = RULE_ORDER) -> list[Rule]:
    """Bind the named rules to `owner`'s methods, in order.

    `sweep_stamps` takes no state, so it is wrapped to the same shape as
    every other rule — one call, one state.
    """
    bound: list[Rule] = []
    for name in order:
        method = getattr(owner, name)
        bound.append(method if name != "sweep_stamps"
                     else (lambda state, _m=method: _m()))
    return bound
