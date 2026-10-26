"""Waiting for a condition instead of guessing how long it takes.

One duty: poll a predicate until it holds, or the deadline passes. The blind
sleep it replaces was wrong in both directions — too short and the check reads
a stale frame, too long and the round wastes minutes per attempt.
"""
from __future__ import annotations

import time


def wait_for_condition(predicate, timeout: float, interval: float = 0.25) -> bool:
    """Poll `predicate` until it holds, or the deadline passes.

    The predicate is called with no arguments and its truthiness decides. It may
    raise: a raised exception means "not yet", never a crash — the condition is
    often "the server process exists", and a missing process tree raises on some
    platforms.

    The deadline is monotonic (`time.monotonic`), so a clock jump cannot extend
    or shorten the wait.

    Returns True when the predicate held, False on the timeout. The caller reads
    False as "not yet" and decides its own next step — this helper never raises
    on a timeout, because "the condition did not arrive" is a normal answer, not
    an error.
    """
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        try:
            if predicate():
                return True
        except Exception:
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(max(0.01, interval))
