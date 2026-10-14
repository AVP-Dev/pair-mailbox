#!/usr/bin/env python3
"""Roster self-healing: pane rechecks plus dead-record TTL (#19.1, #19.3).

19.1: the recorded neighbour pane rechecks on every call, not only at registering
and not only when our own pane misses. A rotten/foreign/busy pane at a dead pid
drops alone, with a transcript record.
19.3: a paneless record with a dead pid expires past the silence TTL (last_seen
older than ROLE_TTL_SECONDS). The authority holder and records with no last_seen
never drop by timer.
"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pair_test_base import PairTestCase  # noqa: E402


def old_seen(days: float = 8.0) -> str:
    return (datetime.now(timezone.utc)
            - timedelta(days=days)).isoformat(timespec="seconds")


def fresh_seen() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class RecheckPeersCase(unittest.TestCase):
    "Clean recheck_peers decisions: with no server, on stubbed checks."

    def heal(self, roles, self_role="me", **kw):
        import roster_heal
        args = {"pid_alive": lambda pid: False,
                "pane_exists": lambda pane: True,
                "belongs_to_project": lambda pane, root: (True, ""),
                "same_pane": lambda a, b: a == b}
        args.update(kw)
        return roster_heal.recheck_peers(roles, self_role, Path("/repo"), **args)

    def test_stale_pane_of_dead_peer_is_dropped(self) -> None:
        roles = {"me": {"pane": "%1", "pid": 111},
                 "ghost": {"pane": "%9", "pid": 999999}}
        events = self.heal(roles, pane_exists=lambda pane: pane != "%9")
        self.assertNotIn("pane", roles["ghost"])
        self.assertEqual([e["kind"] for e in events], ["pane_stale_dropped"])

    def test_live_pane_of_dead_peer_is_kept(self) -> None:
        "A pane alive and ours means the record awaits the process return, never dropping."
        roles = {"me": {"pane": "%1", "pid": 111},
                 "away": {"pane": "%2", "pid": 999999}}
        events = self.heal(roles)
        self.assertEqual(roles["away"].get("pane"), "%2")
        self.assertEqual(events, [])

    def test_live_pid_is_never_touched(self) -> None:
        roles = {"me": {"pane": "%1", "pid": 111},
                 "busy": {"pane": "%404", "pid": 222}}
        events = self.heal(roles, pid_alive=lambda pid: True)
        self.assertEqual(roles["busy"].get("pane"), "%404")
        self.assertEqual(events, [])

    def test_foreign_pane_is_dropped(self) -> None:
        roles = {"me": {"pane": "%1", "pid": 111},
                 "alien": {"pane": "%7", "pid": 999999}}
        events = self.heal(
            roles, belongs_to_project=lambda pane, root: (False, "foreign"))
        self.assertNotIn("pane", roles["alien"])
        self.assertEqual([e["kind"] for e in events], ["pane_foreign_dropped"])

    def test_pinned_pane_is_never_touched(self) -> None:
        "A human pin beats any automation."
        roles = {"me": {"pane": "%1", "pid": 111},
                 "held": {"pane": "%9", "pid": 999999, "pane_pin": "%9"}}
        events = self.heal(roles, pane_exists=lambda pane: False)
        self.assertEqual(roles["held"].get("pane"), "%9")
        self.assertEqual(events, [])

    def test_pane_held_by_a_living_role_is_dropped(self) -> None:
        roles = {"me": {"pane": "%1", "pid": 111},
                 "live": {"pane": "%2", "pid": 222},
                 "ghost": {"pane": "%2", "pid": 999999}}
        events = self.heal(roles, pid_alive=lambda pid: pid == 222)
        self.assertNotIn("pane", roles["ghost"])
        self.assertEqual(roles["live"].get("pane"), "%2")
        self.assertEqual([e["kind"] for e in events], ["pane_stale_dropped"])

    def test_uncheckable_pane_survives(self) -> None:
        "A thrown check means fail-closed, the record stays (lesson #10)."
        def boom(pane):
            raise OSError("tmux went missing")
        roles = {"me": {"pane": "%1", "pid": 111},
                 "ghost": {"pane": "%9", "pid": 999999}}
        events = self.heal(roles, pane_exists=boom,
                           belongs_to_project=mock.Mock(side_effect=OSError))
        self.assertEqual(roles["ghost"].get("pane"), "%9")
        self.assertEqual(events, [])


class ExpireDeadRolesCase(unittest.TestCase):
    def expire(self, roles, writer=None, **kw):
        import roster_heal
        args = {"pid_alive": lambda pid: False}
        args.update(kw)
        return roster_heal.expire_dead_roles(roles, writer, **args)

    def test_expired_role_is_removed_with_transcript_event(self) -> None:
        roles = {"ghost": {"pid": 999999, "last_seen": old_seen()}}
        events = self.expire(roles)
        self.assertNotIn("ghost", roles)
        self.assertEqual([e["kind"] for e in events], ["role_expired"])

    def test_fresh_silence_survives(self) -> None:
        roles = {"resting": {"pid": 999999, "last_seen": fresh_seen()}}
        events = self.expire(roles)
        self.assertIn("resting", roles)
        self.assertEqual(events, [])

    def test_role_with_a_pane_never_expires(self) -> None:
        roles = {"away": {"pane": "%2", "pid": 999999,
                          "last_seen": old_seen(days=30)}}
        events = self.expire(roles)
        self.assertIn("away", roles)
        self.assertEqual(events, [])

    def test_writer_never_expires(self) -> None:
        "Write authority never evaporates by timer (as in prune)."
        roles = {"boss": {"pid": 999999, "last_seen": old_seen(days=30)}}
        events = self.expire(roles, writer="boss")
        self.assertIn("boss", roles)
        self.assertEqual(events, [])

    def test_role_without_last_seen_survives(self) -> None:
        "Age unmeasurable means fail-closed, never deleting."
        roles = {"legacy": {"pid": 999999}}
        events = self.expire(roles)
        self.assertIn("legacy", roles)
        self.assertEqual(events, [])

    def test_live_pid_never_expires(self) -> None:
        roles = {"busy": {"pid": os.getpid(), "last_seen": old_seen(days=30)}}
        events = self.expire(roles, pid_alive=lambda pid: pid == os.getpid())
        self.assertIn("busy", roles)
        self.assertEqual(events, [])


class HealThroughRegisterCase(PairTestCase):
    "The end-to-end path: _register calls heal on every tool."

    def test_stale_peer_pane_is_healed_on_unrelated_call(self) -> None:
        "19.1: our own call with a valid pane heals a rotten neighbour."
        import server
        self.allow_panes()
        self.store.mutate(lambda s: s["roles"].update({
            "ghost": {"pane": "%999991", "pid": 999991,
                      "last_seen": fresh_seen(), "first_seen": fresh_seen()},
        }))
        with mock.patch.object(server, "pane_exists",
                               side_effect=lambda p: p != "%999991"):
            self.writer.tool_pair_state()
        roles = self.store.read()["roles"]
        self.assertNotIn("pane", roles["ghost"])
        kinds = [e["kind"] for e in self.store.read_transcript()]
        self.assertIn("pane_stale_dropped", kinds)

    def test_expired_peer_is_removed_on_unrelated_call(self) -> None:
        "19.3: a paneless dead record expires on a foreign call."
        import roster_heal
        import server
        self.allow_panes()
        self.store.mutate(lambda s: s["roles"].update({
            "dust": {"pid": 999992, "last_seen": old_seen(),
                     "first_seen": old_seen()},
        }))
        with mock.patch.object(roster_heal, "ROLE_TTL_SECONDS", 1.0):
            with mock.patch.object(server, "pane_exists", return_value=True):
                self.writer.tool_pair_state()
        roles = self.store.read()["roles"]
        self.assertNotIn("dust", roles)
        kinds = [e["kind"] for e in self.store.read_transcript()]
        self.assertIn("role_expired", kinds)


if __name__ == "__main__":
    unittest.main()
