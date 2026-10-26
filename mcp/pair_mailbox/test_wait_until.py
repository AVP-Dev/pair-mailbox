"""Waiting tests (`mcp/pair_mailbox/wait_until.py`).

One test file per source: `wait_until.py` owns one duty — poll a predicate
until it holds — and it is checked here.

The behaviour that matters to the callers: the wait ends the moment the
condition holds, it gives up at the deadline instead of hanging, and a
predicate that raises means "not yet" rather than a crash. The last one is the
reason the helper exists in this shape: the condition is often "the server
process exists", and reading a process tree raises on some platforms.
"""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from wait_until import wait_for_condition  # noqa: E402


class TestWaitForCondition(unittest.TestCase):
    def test_it_returns_as_soon_as_the_predicate_holds(self) -> None:
        state = {"n": 0}

        def ready() -> bool:
            state["n"] += 1
            return state["n"] >= 3

        started = time.monotonic()
        self.assertTrue(wait_for_condition(ready, timeout=5, interval=0.01))
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(3, state["n"])

    def test_it_gives_up_at_the_deadline(self) -> None:
        started = time.monotonic()
        self.assertFalse(wait_for_condition(lambda: False, timeout=0.2, interval=0.05))
        self.assertGreaterEqual(time.monotonic() - started, 0.2)

    def test_a_raising_predicate_means_not_yet(self) -> None:
        calls = {"n": 0}

        def flaky() -> bool:
            calls["n"] += 1
            if calls["n"] < 3:
                raise OSError("no process tree")
            return True

        self.assertTrue(wait_for_condition(flaky, timeout=5, interval=0.01))

    def test_a_zero_timeout_asks_once_and_gives_up(self) -> None:
        """No budget means no waiting — but the condition is still asked once.

        A zero timeout that never asked would report "not ready" about a state it
        never looked at.
        """
        calls = {"n": 0}

        def never() -> bool:
            calls["n"] += 1
            return False

        self.assertFalse(wait_for_condition(never, timeout=0, interval=0.01))
        self.assertEqual(1, calls["n"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
