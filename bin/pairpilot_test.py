#!/usr/bin/env python3
"""Supervisor tests: they check what it DOES, not that the file exists.

Each test raises its own state in tmp and looks at the result: did the deferred
message appear, is the event in the transcript, is the report written.

Run:  python3 bin/pairpilot_test.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "mcp" / "pair_mailbox"))

_spec = importlib.util.spec_from_loader("pairpilot", None)
pairpilot = importlib.util.module_from_spec(_spec)
pairpilot.__dict__["__file__"] = str(ROOT / "bin" / "pairpilot")
exec(compile((ROOT / "bin" / "pairpilot").read_text(encoding="utf-8"), "pairpilot", "exec"), pairpilot.__dict__)


def now_iso() -> str:
    """The current moment in the same format the server writes into `last_seen`.

    A separate function instead of hand-editing state: comparing runs on strings,
    and "mismatch" here means "the role never woke". A hand literal would read
    right only today.
    """
    return datetime.now(timezone.utc).isoformat()


def ago(seconds: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


class PilotCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.tmp.name) / ".pair"
        self.state_dir.mkdir(parents=True)
        # TMUX_PANE drops for the test and returns after. In test_protocol.py this came
        # from F-08 (the suite must never spam the operator window with real output),
        # but here the reason is other and nastier: the obligation tests use pane %20,
        # and running the suite FROM %20 the pilot takes agy for sitting in its own pane,
        # adds it to skip and stays silent. The suite fell two tests depending on which
        # pane launched it — the check depended on place, not code.
        # pane launched it — the check depended on place, not code.
        self._orig_tmux_pane = os.environ.pop("TMUX_PANE", None)

    def tearDown(self) -> None:
        if self._orig_tmux_pane is not None:
            os.environ["TMUX_PANE"] = self._orig_tmux_pane
        else:
            os.environ.pop("TMUX_PANE", None)
        self.tmp.cleanup()

    def pilot(self, silence: float = 600.0) -> "pairpilot.Pilot":
        # tmux switches off: tests must never touch live windows.
        # Profiles isolate too: the dynamic `launch_command` reads the real
        # `cli-profiles.json`, and the "no command means no restart" test would fall on
        # a live tree where the `agy` profile exists.
        p = pairpilot.Pilot(self.state_dir, silence=silence, interval=1)
        p.tmux = None
        orig = p._launch_commands
        here = self.state_dir
        def isolated(given=None):
            saved = pairpilot.ROOT
            try:
                pairpilot.ROOT = here
                return orig(given)
            finally:
                pairpilot.ROOT = saved
        p._launch_commands = isolated
        return p

    def write_state(self, **fields) -> dict:
        state = {
            "task": "test",
            "mode": "fix",
            "writer": None,
            "phase": "implement",
            "round": 1,
            "roles": {},
            "seen": {},
            "patches": {},
            # An explicit empty list: state speaks the new language, and the old task line
            # never rises back as an open task. Or every test in this file would take a
            # "task undone" blocker.
            "tasks": [],
        }
        state.update(fields)
        (self.state_dir / "state.json").write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        return state

    def wake(self, pilot: "pairpilot.Pilot", role: str) -> dict:
        """A recorded role wake, or {} when missing."""
        path = pilot.wake_root / f"{role}.json"
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def assert_enqueued_for_push(self, pilot: "pairpilot.Pilot", role: str) -> None:
        """The postman pushes at once: the queue is NOT empty past an unread.

        The old contract demanded silence till escalation (a wake file, polling in 4
        min, then printing) — a sleeping agent learned the letter in minutes, and
        the pair worked only while the human pushed by hand. New contract: memory
        (wake) and push (outbox) write together, and this same tick flush drives the
        push into a free composer.
        """
        self.assertTrue(pilot.pending(role),
                        "an unread must land in the push queue at once")

    def inbox(self, role: str, count: int) -> None:
        path = self.state_dir / "mailbox" / role
        path.mkdir(parents=True, exist_ok=True)
        for index in range(count):
            (path / f"00000{index}-x.json").write_text("{}", encoding="utf-8")

    def transcript_kinds(self) -> list[str]:
        path = self.state_dir / "transcript.jsonl"
        if not path.exists():
            return []
        return [json.loads(line)["kind"] for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def read_state(self) -> dict:
        """State from disk, not pilot memory.

        What must check is the roster on disk, not the last mutate return: or the
        pid-update claim would check the test itself.
        """
        return json.loads((self.state_dir / "state.json").read_text(encoding="utf-8"))

    def transcript_entries(self) -> list[dict]:
        path = self.state_dir / "transcript.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class TestWakeAdapterCall(PilotCase):
    """The pilot hands the wake adapter the exact recorded session id.

    Round 101c: wake_opencode.wake grew a required `session_id` and the pilot
    still called the old two-arg signature - a pilot wake would have died as
    pilot_wake_api_failed forever. The pin: the call is three-arg and the id
    comes from the state roster, never a guess.
    """

    def test_ring_passes_recorded_session_id(self) -> None:
        """The recorded session id rides to the adapter, never a guess."""
        pilot = self.pilot()
        pilot.tmux = "tmux-fake"  # ring gates on a live pane; pane_alive is mocked
        state = self.write_state(roles={"opencode": {
            "pane": "%7", "opencode_session_id": "ses_recorded"}})
        captured = {}

        class FakeAdapter:
            @staticmethod
            def wake(project_root, pointer, session_id=None):
                captured["args"] = (project_root, pointer, session_id)
                return {"woken": False, "why": "test adapter"}

        with mock.patch.object(pairpilot, "pane_alive", return_value=True), \
                mock.patch.object(pairpilot.subprocess, "run"), \
                mock.patch.dict(sys.modules, {"wake_opencode": FakeAdapter}):
            pilot.ring(state, "opencode", "2 unread")
        self.assertIn("args", captured, "the adapter must be called")
        root, pointer, session_id = captured["args"]
        self.assertEqual("ses_recorded", session_id,
                         "the recorded session id rides to the adapter")
        self.assertIn("[pair]", pointer)
        self.assertEqual(str(pilot.store.project_root), str(root))

    def test_ring_without_a_recorded_session_still_calls_fail_closed(self) -> None:
        """Absent id passes None; the adapter refuses, never guesses."""
        pilot = self.pilot()
        pilot.tmux = "tmux-fake"
        state = self.write_state(roles={"opencode": {"pane": "%7"}})
        captured = {}

        class FakeAdapter:
            @staticmethod
            def wake(project_root, pointer, session_id=None):
                captured["session_id"] = session_id
                return {"woken": False, "why": "no recorded OpenCode session id"}

        with mock.patch.object(pairpilot, "pane_alive", return_value=True), \
                mock.patch.object(pairpilot.subprocess, "run"), \
                mock.patch.dict(sys.modules, {"wake_opencode": FakeAdapter}):
            pilot.ring(state, "opencode", "2 unread")
        self.assertIsNone(captured["session_id"])
        self.assertIn("pilot_wake_api", self.transcript_kinds())



class TestWakeAckContract(PilotCase):
    """(d) A wake resolves on unread DECREASE, never on any tool call.

    Round 101 live false ack (08:08:12): a role with unread mail called any
    pair_* tool, last_seen moved, `woke` fired and the wake file died - mail
    never read. The clause that read `seen_now != seen_ask` measured activity,
    not consumption.
    """

    def _wake_file(self, pilot, role="agy"):
        self.inbox(role, 2)
        self.write_state(writer=None, roles={role: {
            "last_seen": ago(3600), "pid": 1, "pane": "%1"}})
        pilot.tick()
        path = pilot.wake_root / f"{role}.json"
        self.assertTrue(path.exists(), "the wake must be raised")
        return path

    def test_last_seen_movement_alone_never_acknowledges(self) -> None:
        pilot = self.pilot()
        path = self._wake_file(pilot)
        # The role calls tools: last_seen moves, unread stays 2.
        self.write_state(writer=None, roles={"agy": {
            "last_seen": now_iso(), "pid": 1, "pane": "%1"}})
        pilot.tick()
        self.assertTrue(path.exists(),
                        "activity without consumption keeps the wake alive")
        self.assertNotIn("pilot_wake_acknowledged", self.transcript_kinds())

    def test_unread_decrease_acknowledges(self) -> None:
        pilot = self.pilot()
        path = self._wake_file(pilot)
        # The role reads one letter: unread drops 2 -> 1.
        state = self.write_state(writer=None,
                                 roles={"agy": {"last_seen": now_iso(),
                                                "pid": 1, "pane": "%1"}},
                                 seen={"agy": 1})
        pilot.tick()
        self.assertFalse(path.exists(),
                         "a real unread decrease is the ack")
        self.assertIn("pilot_wake_acknowledged", self.transcript_kinds())

    def test_parked_round_never_false_acknowledges_or_escalates(self) -> None:
        """Parked in done: no escalation AND no last_seen-based ack - the two
        fixes must not undo each other (ordering pin)."""
        pilot = self.pilot()
        path = self._wake_file(pilot)
        self.write_state(writer=None, phase="done", tasks=[],
                         roles={"agy": {"last_seen": now_iso(), "pid": 1,
                                        "pane": "%1"}}, seen={})
        pilot.tick()
        self.assertTrue(path.exists(),
                        "parked means untouched: neither acked nor escalated")
        kinds = self.transcript_kinds()
        self.assertNotIn("pilot_wake_acknowledged", kinds)
        self.assertNotIn("pilot_wake_escalated", kinds)

    def test_stale_wake_file_drops_with_a_named_record(self) -> None:
        """A wake nobody consumed past the TTL drops, naming file, age and ask."""
        pilot = self.pilot()
        pilot.wake_grace = 10**9  # never escalate: only the TTL path may act
        pilot.outbox_ttl = 60.0
        path = self._wake_file(pilot)
        old = time.time() - 120
        os.utime(path, (old, old))
        pilot.tick()
        self.assertFalse(path.exists(), "the stale pointer drops")
        drops = [e for e in self.transcript_entries()
                 if e.get("kind") == "pilot_wake_expired"]
        self.assertEqual(1, len(drops))
        self.assertEqual("agy", drops[0].get("target"))
        self.assertGreaterEqual(drops[0].get("age_seconds", 0), 60)
        self.assertIn("unread_at_ask", drops[0])

    def test_parked_round_drops_queued_items(self) -> None:
        """Parked in done: queued pointers drop at once, not at TTL.

        The parked gate existed in poll_wakes and unread_peers but not in the
        flush path: an item queued before `done` hung for its whole lifetime
        and landed as fresh mail about a finished round.
        """
        pilot = self.pilot()
        pilot.tmux = "tmux-fake"
        self.write_state(writer=None, roles={"agy": {"last_seen": now_iso(),
                                                     "pid": 1, "pane": "%1"}})
        pilot.enqueue("agy", "old pointer text")
        self.assertEqual("old pointer text", pilot.pending("agy"))
        self.write_state(writer=None, phase="done", tasks=[],
                         roles={"agy": {"last_seen": now_iso(), "pid": 1,
                                        "pane": "%1"}}, seen={})
        with mock.patch.object(pairpilot, "pane_alive", return_value=True), \
                mock.patch.object(pairpilot, "deliver",
                                  return_value={"delivered": True}):
            pilot.tick()
        self.assertEqual("", pilot.pending("agy"),
                         "a parked round empties the queue in the same tick")
        self.assertIn("pilot_outbox_parked_drop", self.transcript_kinds())


class TestWakeFileIntegrity(PilotCase):
    """A wake is one file per role, never losing what it gathered.

    Two wake reasons (`unread_peers` and `stale_writer`) share one file.
    Overwriting read harmless till counted: it zeroed `asked_at`, so patience
    restarted, and wiped `escalated`, so the same fact escalated twice — on the
    first reason, then on the second.
    """

    def test_since_seconds_is_measured_before_the_file_is_removed(self) -> None:
        """`since_seconds` must mean something.

        The order was mixed: `unlink` ran before `stat`, and `exists()` past removal
        always reads `False`, so the field always stood 0 — telling nothing about how
        long the role stayed silent. The file ages on purpose here, or rounding to
        whole seconds would hide the zero bug.
        """
        self.write_state(writer="agy", seen={"agy": 0},
                         roles={"agy": {"last_seen": ago(3600), "pid": 1, "pane": "%1"}})
        self.inbox("agy", 2)
        pilot = self.pilot()
        pilot.tick()
        path = pilot.wake_root / "agy.json"
        self.assertTrue(path.exists())
        old = time.time() - 600
        os.utime(path, (old, old))
        # The role consumes one letter: unread drops, the wake resolves.
        self.write_state(writer="agy", seen={"agy": 1},
                         roles={"agy": {"last_seen": now_iso(), "pid": 1, "pane": "%1"}})
        pilot.tick()
        entries = [e for e in self.transcript_entries()
                   if e.get("kind") == "pilot_wake_acknowledged"]
        self.assertEqual(1, len(entries))
        self.assertGreaterEqual(
            entries[0]["since_seconds"], 590,
            "silence measures before the file removal, or the field always reads zero")

    def test_last_seen_movement_without_consumption_keeps_the_wake(self) -> None:
        """[R101d] The inverse pin of the contract: activity is not consumption.

        The old shape of the test above satisfied itself by moving last_seen
        alone - exactly the live false ack (08:08:12) this contract removes.
        """
        self.write_state(writer="agy", roles={"agy": {"last_seen": ago(3600), "pid": 1, "pane": "%1"}})
        self.inbox("agy", 2)
        pilot = self.pilot()
        pilot.tick()
        path = pilot.wake_root / "agy.json"
        self.assertTrue(path.exists())
        old = time.time() - 600
        os.utime(path, (old, old))
        self.write_state(writer="agy", roles={"agy": {"last_seen": now_iso(), "pid": 1, "pane": "%1"}})
        pilot.tick()
        self.assertTrue(path.exists(),
                        "last_seen moved, unread unchanged - the wake stays")
        self.assertEqual([], [e for e in self.transcript_entries()
                              if e.get("kind") == "pilot_wake_acknowledged"])

    def test_two_reasons_escalate_once_not_twice(self) -> None:
        """Two wake reasons — one escalation.

        A semantics check, not a call-list one: raising `wake_grace` to zero never
        suffices, both reasons must fire in one round on one role.
        """
        self.inbox("agy", 3)
        self.write_state(writer="agy", roles={"agy": {"last_seen": ago(3600), "pid": 1, "pane": "%1"}})
        pilot = self.pilot()
        pilot.wake_grace = 0.0
        pilot.tick()
        pilot.tick()
        pilot.tick()
        kinds = [e.get("kind") for e in self.transcript_entries()]
        self.assertGreaterEqual(
            kinds.count("pilot_wake_escalated"), 1,
            "at least one escalation must stand")
        self.assertEqual(
            1, kinds.count("pilot_wake_escalated"),
            "unreads and the writer silence — one wake fact, "
            "not two")

    def test_a_pending_wake_keeps_its_first_moment(self) -> None:
        """`asked_at` is the question moment, not the last write.

        Refreshing the counter from when the role should already have woken makes
        "patience" endless: an unanswered question takes a full wait again.
        """
        # `last_seen` holds ONE line for the whole test: `ago()` gives different
        # microseconds per call, and the role reads "woken" by itself — checking the
        # wrong thing.
        quiet = ago(3600)
        self.write_state(writer="agy", roles={"agy": {"last_seen": quiet, "pid": 1, "pane": "%1"}})
        pilot = self.pilot()
        pilot.tick()
        first = self.wake(pilot, "agy")
        self.assertTrue(first)
        first_asked = first["asked_at"]
        # A second reason on the same role, with no answer.
        self.inbox("agy", 2)
        pilot.tick()
        second = self.wake(pilot, "agy")
        self.assertTrue(second, "a second reason must never wipe a pending wake")
        self.assertEqual(first_asked, second["asked_at"],
                         "patience counts from the first question")
        self.assertIn("unread", " ".join(second["reasons"]),
                      "a second reason must show in the same file")

    def test_a_damaged_wake_file_is_removed_not_reread(self) -> None:
        """A damaged wake file is removed, not re-read every tick.

        Without removal every tick fails parsing, writes `pilot_wake_damaged`
        and keeps the file — an endless transcript loop about the same file.
        Full `tick()` (no unread mail, so nothing overwrites the fixture):
        the damaged file is gone after the tick and reported once.
        """
        self.write_state(writer="agy", roles={"agy": {"last_seen": now_iso(), "pid": 1, "pane": "%1"}})
        pilot = self.pilot()
        damaged = pilot.wake_root / "agy.json"
        damaged.parent.mkdir(parents=True, exist_ok=True)
        damaged.write_text("{not json", encoding="utf-8")
        pilot.tick()
        self.assertFalse(damaged.exists(),
                         "the damaged file must be gone after the tick")
        self.assertIn("pilot_wake_damaged", self.transcript_kinds(),
                      "the damaged file must reach the transcript")
        before = len(self.transcript_entries())
        pilot.tick()
        self.assertEqual(before, len(self.transcript_entries()),
                         "the second tick writes nothing about the same file: it is gone")

    def test_a_damaged_wake_file_overwritten_still_reports(self) -> None:
        """A damaged file healed by overwrite (unread=1) still reports.

        `mark_wake` reads the old file to amend it; a damaged one used to be
        replaced silently, so \"were there damaged wakes\" depended on whether
        the role had unread mail that tick. Now both paths record
        `pilot_wake_damaged`.
        """
        self.inbox("agy", 1)
        self.write_state(writer="agy", roles={"agy": {"last_seen": now_iso(), "pid": 1, "pane": "%1"}})
        pilot = self.pilot()
        damaged = pilot.wake_root / "agy.json"
        damaged.parent.mkdir(parents=True, exist_ok=True)
        damaged.write_text("{not json", encoding="utf-8")
        pilot.tick()
        self.assertIn("pilot_wake_damaged", self.transcript_kinds(),
                      "overwrite-heal must not swallow the damage report")



class TestLiveness(PilotCase):
    def test_dead_role_without_pane_takes_no_push(self) -> None:
        """A dead pid with no pane: unread mail writes no wake, no outbox.

        Live 2026-10-15 (round 99): agy/pi/claude held dead pids and pane
        None with unread mail stacked — every tick wrote a wake file plus
        an outbox escort that flush could never deliver (pane None reads
        unproven, kept). An eternal loop over nobody."""
        self.write_state(roles={"agy": {"last_seen": ago(3600), "pid": 998111}},
                         seen={"agy": 0})
        self.inbox("agy", 2)
        pilot = self.pilot()
        pilot.tick()
        self.assertEqual({}, self.wake(pilot, "agy"),
                         "no wake file for a role nobody can reach")
        self.assertEqual("", pilot.pending("agy"),
                         "no outbox escort for a role nobody can reach")

    def test_dead_role_wake_file_drops_instead_of_escalating(self) -> None:
        """A hanging wake of a dead role drops with a record, never re-arms.

        poll_wakes used to re-escalate past the pause forever: enqueue into
        a file flush keeps as unproven, wake file re-armed, repeat. The
        drop lands in the transcript as pilot_wake_dead_role."""
        self.write_state(roles={"agy": {"last_seen": ago(3600), "pid": 998111}})
        pilot = self.pilot()
        pilot.wake_grace = 0.0
        pilot.mark_wake(pilot.state(), "agy", "2 unread", "read your mail")
        wake_file = pilot.wake_root / "agy.json"
        payload = json.loads(wake_file.read_text(encoding="utf-8"))
        payload["asked_at"] = ago(3600)
        wake_file.write_text(json.dumps(payload), encoding="utf-8")
        pilot.tick()
        pilot.tick()
        self.assertFalse(wake_file.exists(),
                         "the dead-role wake must drop, not re-arm")
        self.assertEqual("", pilot.pending("agy"),
                         "no escalation enqueue for a dead role")
        self.assertIn("pilot_wake_dead_role", self.transcript_kinds())
    def test_dead_role_outbox_drops_instead_of_keeping(self) -> None:
        """A queued escort of a dead role drops with a record, never kept.

        flush_outbox held pane-None files as unproven every tick (log
        'keeping for next tick' forever). With role_pushable the file
        drops as pilot_outbox_dead_role."""
        p = self.pilot()
        p.enqueue("agy", "queued nudge")
        path = p.outbox / "agy.txt"
        state = self.write_state(roles={"agy": {"pane": None, "pid": 998111}})
        calls = []
        orig_deliver = pairpilot.deliver
        orig_belongs = pairpilot.pane_belongs_to_project
        orig_alive = pairpilot.pane_alive
        pairpilot.deliver = lambda pane, text, wait_for_empty=0.0: calls.append((pane, text)) or {"delivered": True}
        pairpilot.pane_belongs_to_project = lambda tmux, pane, root: (_ for _ in ()).throw(RuntimeError("no pane"))
        pairpilot.pane_alive = lambda tmux, pane: False
        try:
            p.tmux = "tmux"
            p.flush_outbox(state)
        finally:
            pairpilot.deliver = orig_deliver
            pairpilot.pane_belongs_to_project = orig_belongs
            pairpilot.pane_alive = orig_alive
        self.assertEqual(calls, [], "nothing printed for a dead role")
        self.assertFalse(path.exists(), "the dead-role escort drops, not kept")
        self.assertIn("pilot_outbox_dead_role", self.transcript_kinds())


    def test_silent_writer_is_woken_silently(self) -> None:
        """A silent writer: the wake records, nothing prints to the composer.

        The check grew stricter than before. It used to demand the reminder land in
        the print queue — meaning the supervisor climbs into a foreign window. Now
        the reverse: the fact stands, the foreign window stays empty. Had the fix
        printed, the test would fall, never grow greener.
        """
        self.write_state(writer="agy", roles={"agy": {"last_seen": ago(3600), "pid": 1, "pane": "%1"}})
        pilot = self.pilot()
        pilot.tick()
        wake = self.wake(pilot, "agy")
        self.assertTrue(wake, "a silent writer must take a wake")
        self.assertIn("phase", wake["reason"])
        self.assertIn("write", wake["text"])
        self.assert_enqueued_for_push(pilot, "agy")

    def test_a_writer_who_woke_up_is_not_printed_at(self) -> None:
        """A woken one never wakes twice — with a record of it.

        It used to read same as "delivered and forgotten": the print queue cleared
        past success and past a dropped-by-deadline alike. Now the outcome shows in
        the journal, proving no printing was needed.
        [R101d] "Woke up" reads as unread DECREASE: last_seen moves on every
        tool call and is not evidence any mail was consumed.
        """
        self.write_state(writer="agy", roles={"agy": {"last_seen": ago(3600), "pid": 1, "pane": "%1"}})
        self.inbox("agy", 2)
        pilot = self.pilot()
        pilot.tick()
        self.assertTrue(self.wake(pilot, "agy"))
        # The role consumes one letter: unread 2 -> 1.
        self.write_state(writer="agy", seen={"agy": 1},
                         roles={"agy": {"last_seen": now_iso(), "pid": 1, "pane": "%1"}})
        pilot.tick()
        self.assertEqual({}, self.wake(pilot, "agy"), "the wake drops")
        kinds = [e.get("kind") for e in self.transcript_entries()]
        self.assertIn("pilot_wake_acknowledged", kinds)
        self.assert_enqueued_for_push(pilot, "agy")

    def test_a_writer_who_ignores_the_wake_is_printed_at_once(self) -> None:
        """Escalation: printing turns the exception, not the first step.

        The order here IS the fix content. Had printing run first, a role with a
        long edit would take a composer poke every tick; now it first takes a chance
        to wake alone, and printing arrives once and late — when silence already
        risks the round.
        """
        self.write_state(writer="agy", roles={"agy": {"last_seen": ago(3600), "pid": 1, "pane": "%1"}})
        pilot = self.pilot()
        pilot.wake_grace = 0.0
        pilot.tick()
        pilot.tick()
        printed = pilot.pending("agy")
        self.assertTrue(printed, "past the patience deadline printing must stand")
        self.assertIn("write", printed)
        pilot.tick()
        pilot.tick()
        self.assertEqual(printed, pilot.pending("agy"),
                         "escalation prints once, never every tick")
        kinds = [e.get("kind") for e in self.transcript_entries()]
        self.assertEqual(1, kinds.count("pilot_wake_escalated"),
                         "escalation must never repeat every tick")

    def test_escalation_repeats_after_a_long_silence(self) -> None:
        """Escalation must repeat.

        The old contract read "exactly once per role life", and it trapped with no
        exit: the file drops only when the role WAKES, so a silent role takes no
        second escalation in principle. Found live: pi stayed silent an hour, the
        file hung with escalated, the supervisor never printed.
        """
        self.write_state(writer="agy", roles={"agy": {"last_seen": ago(3600), "pid": 1, "pane": "%1"}})
        pilot = self.pilot()
        pilot.wake_grace = 0.0
        pilot.tick()
        pilot.tick()
        self.assertTrue(pilot.pending("agy"), "the first escalation must stand")
        # The queue clears: or the next check would see not its own escalation but the
        # writer-idle push — another reason and another text. Exactly the wake repeat
        # checks.
        pilot.outbox_file("agy").unlink(missing_ok=True)

        # The repeat pause never passed — ticking often, no repeat must come.
        for _ in range(5):
            pilot.tick()
        self.assertEqual("", pilot.pending("agy"),
                         "repeating inside the pause is noise, banned")

        # The pause passed, the role never woke — a repeat must come.
        wake_file = pilot.wake_root / "agy.json"
        payload = json.loads(wake_file.read_text(encoding="utf-8"))
        payload["escalated_at"] = ago(int(pairpilot.WAKE_ESCALATE_REPEAT_SECONDS) + 60)
        wake_file.write_text(json.dumps(payload), encoding="utf-8")

        pilot.tick()
        self.assertTrue(pilot.pending("agy"),
                        "a role silent past the pause means a repeat escalation must come")
        kinds = [e.get("kind") for e in self.transcript_entries()]
        self.assertGreaterEqual(kinds.count("pilot_wake_escalated"), 2,
                                "the repeat must land in the transcript")
        repeats = [e for e in self.transcript_entries()
                   if e.get("kind") == "pilot_wake_escalated" and e.get("repeat")]
        self.assertEqual(1, len(repeats), "the repeat marks separately in the transcript")

    def test_outbox_ttl_outlives_the_escalation_pause(self) -> None:
        """Queue TTL must outrun the escalation repeat pause.

        No taste preference but a working condition. At TTL 600 with pause 900 the
        escalation rotted in the queue before the repeat landed in it: the pilot
        wrote "dropped, awaited the composer 15 min", with nothing left to repeat.
        Found on a live session — the repeat escalation never reached a composer.

        Constants compare here, not behaviour, because behaviour is the consequence:
        delivery falls first, noticed late.
        """
        self.assertGreater(
            pairpilot.DEFAULT_OUTBOX_TTL, pairpilot.WAKE_ESCALATE_REPEAT_SECONDS,
            "a queue TTL shorter than the escalation pause: the repeat never arrives")

    def test_escalation_survives_long_enough_to_repeat(self) -> None:
        """Escalation must outlive its own wait.

        A live-path check: escalation lays text in the queue, and the queue must
        never drop it before the repeat.
        """
        self.write_state(writer="agy", roles={"agy": {"last_seen": ago(3600), "pid": 1, "pane": "%1"}})
        pilot = self.pilot()
        pilot.wake_grace = 0.0
        pilot.tick()
        pilot.tick()
        path = pilot.outbox_file("agy")
        self.assertTrue(path.exists(), "escalation must land in the queue")
        age = time.time() - path.stat().st_mtime
        self.assertLess(age, pilot.outbox_ttl,
                        "escalation must never rot in its own queue")

    def test_fresh_writer_is_left_alone(self) -> None:
        self.write_state(writer="agy", roles={"agy": {"last_seen": ago(30), "pane": "%1"}})
        pilot = self.pilot()
        pilot.tick()
        self.assertEqual({}, self.wake(pilot, "agy"), "silence inside the norm stays untouched")

    def test_nudge_is_not_queued_when_round_is_done(self) -> None:
        self.write_state(writer="agy", phase="done", closed_at=ago(10),
                         roles={"agy": {"last_seen": ago(3600), "pane": "%1"}})
        pilot = self.pilot()
        pilot.tick()
        self.assertEqual("", pilot.pending("agy"), "a closed round never wakes")

    def test_silent_role_with_no_input_is_nudged_once(self) -> None:
        """A silent role with no incoming in a live round means no link.

        The reverse pin used to stand here: "critic silence is no defect, maybe
        nothing to say". The 2026-10-11 measure showed where it ends: in 2 of 8
        rounds the writer never wrote the neighbour till the phase change, and
        nobody woke the critic — the pilot held no path for him. The old contract
        drops, the new one: the silence threshold that only the writer owned
        (`stale_writer`) symmetrically covers a role with no incoming. One push a
        round, the reason — `pilot_idle_role`.
        """
        self.write_state(writer="agy", roles={"agy": {"last_seen": ago(30), "pane": "%1"},
                                              "codex": {"last_seen": ago(3600), "pane": "%2"}})
        pilot = self.pilot()
        pilot.tick()
        self.assertTrue(self.wake(pilot, "codex"), "a silent role with no incoming wakes")
        self.assertTrue(pilot.pending("codex"), "the push at once, as with an unread")
        self.assertIn("pilot_idle_role", self.transcript_kinds())
        pilot.tick()
        self.assertEqual(len([k for k in self.transcript_kinds()
                              if k == "pilot_idle_role"]), 1, "no repeat in the same round")

    def test_fresh_role_is_left_alone(self) -> None:
        self.write_state(writer="agy", roles={"agy": {"last_seen": ago(30), "pane": "%1"},
                                              "codex": {"last_seen": ago(60), "pane": "%2"}})
        pilot = self.pilot()
        pilot.tick()
        self.assertEqual({}, self.wake(pilot, "codex"), "silence inside the norm stays untouched")

    def test_role_nudge_is_not_queued_when_round_is_done(self) -> None:
        self.write_state(writer="agy", phase="done", closed_at=ago(10),
                         roles={"agy": {"last_seen": ago(30), "pane": "%1"},
                                "codex": {"last_seen": ago(3600), "pane": "%2"}})
        pilot = self.pilot()
        pilot.tick()
        self.assertEqual("", pilot.pending("codex"), "a closed round never wakes")

    def test_role_with_unread_stays_with_the_mail_path(self) -> None:
        """Mail wakes its own path; a second push of the same text is spam."""
        self.inbox("codex", 2)
        self.write_state(writer="agy", roles={"codex": {"last_seen": ago(3600), "pane": "%1"}},
                         seen={"codex": 0})
        pilot = self.pilot()
        pilot.tick()
        self.assertIn("pilot_unread_nudge", self.transcript_kinds())
        self.assertNotIn("pilot_idle_role", self.transcript_kinds())

    def test_unread_nudge_is_sent_once_and_not_every_tick(self) -> None:
        """An unread reminder pushes at once, but once: the agent ends its own turn
        (PROTOCOL §7), and the push must never turn into a stream that wakes and
        lulls by itself."""
        self.inbox("codex", 2)
        self.write_state(writer="agy", roles={"codex": {"last_seen": ago(60), "pane": "%1"}},
                         seen={"codex": 0})
        pilot = self.pilot()
        pilot.tick()
        self.assertTrue(self.wake(pilot, "codex"))
        pilot.tick()
        pilot.tick()
        # Events count in the journal, not text: the reminder text reads same every
        # time, and line comparing never sees the repeat.
        nudges = [e for e in self.transcript_entries() if e.get("kind") == "pilot_unread_nudge"]
        self.assertEqual(1, len(nudges),
                         "a reminder must never repeat every tick")
        self.assert_enqueued_for_push(pilot, "codex")

    def test_unread_nudge_does_not_touch_a_role_with_nothing_unread(self) -> None:
        """A reminder with no letter is noise, not help."""
        self.write_state(writer="agy", roles={"codex": {"last_seen": ago(60), "pane": "%1"}},
                         seen={"codex": 0})
        pilot = self.pilot()
        pilot.tick()
        self.assertEqual("", pilot.pending("codex"))

    def test_second_letter_after_reading_wakes_again(self) -> None:
        """Read means the stamp drops: a second letter wakes anew.

        The round-43 regression: the unread-{role} stamp lived forever, and a
        second agent letter never woke again.
        """
        self.inbox("codex", 2)
        self.write_state(writer="agy", roles={"codex": {"last_seen": ago(60), "pane": "%1"}},
                         seen={"codex": 0})
        pilot = self.pilot()
        pilot.tick()
        first = [e for e in self.transcript_entries() if e.get("kind") == "pilot_unread_nudge"]
        self.assertEqual(1, len(first))
        # The agent read all: the box empty, seen dropped — as past pair_read.
        for f in (self.state_dir / "mailbox" / "codex").glob("*.json"):
            f.unlink()
        self.write_state(writer="agy", roles={"codex": {"last_seen": ago(60), "pane": "%1"}},
                         seen={"codex": 0})
        pilot.tick()
        # A new letter arrived — the stamp already dropped, waking anew.
        self.inbox("codex", 1)
        pilot.tick()
        nudges = [e for e in self.transcript_entries() if e.get("kind") == "pilot_unread_nudge"]
        self.assertEqual(2, len(nudges),
                         "a second letter past reading must wake anew")

    def test_enqueue_replacement_leaves_a_trace(self) -> None:
        """A queue eviction writes into the journal, never vanishes silent."""
        self.write_state(writer="agy", roles={"codex": {"last_seen": ago(60), "pane": "%1"}})
        pilot = self.pilot()
        pilot.enqueue("codex", "first reminder")
        pilot.enqueue("codex", "second reminder")
        replaced = [e for e in self.transcript_entries()
                    if e.get("kind") == "pilot_outbox_replaced"]
        self.assertEqual(1, len(replaced))
        self.assertIn("first", replaced[0].get("old", ""))
        self.assertEqual("second reminder", pilot.pending("codex"))

    def test_hanging_request_repeats_after_repeat_interval(self) -> None:
        """A hanging request reminds again, never more often than reply_repeat."""
        roles = {"codex": {"last_seen": ago(60), "pane": "%1"},
                 "agy": {"last_seen": ago(60), "pane": "%2"}}
        self.write_state(writer="agy", roles=roles)
        pilot = self.pilot()
        pilot.reply_grace = 0
        pilot.reply_repeat = 3600.0
        root = self.state_dir / "obligations"
        root.mkdir(parents=True, exist_ok=True)
        (root / "000007-q.json").write_text(json.dumps({
            "seq": 7, "from": "agy", "to": "codex",
            "opened_at": ago(120), "subject": "question 7",
        }), encoding="utf-8")
        pilot.tick()
        first_text = pilot.pending("codex")
        self.assertIn("request 7", first_text,
                      "a first reminder of a hanging request must come")
        pilot.tick()
        self.assertEqual(first_text, pilot.pending("codex"),
                         "a repeat before reply_repeat is banned")
        # Age the stamp past the repeat bound — the reminder repeats.
        stamp = pilot.state_dir / "pilot" / f".{pairpilot.stamp_name('reply-7-codex')}"
        old = time.time() - 3700.0
        os.utime(stamp, (old, old))
        pilot.tick()
        self.assertIn("already 2 min", pilot.pending("codex"),
                      "an aged stamp must give a repeat reminder")


class TestUnread(PilotCase):
    def test_unread_mail_produces_a_wake_and_a_push(self) -> None:
        """A letter means memory and push stand: the wake file and the typing queue.

        The old contract demanded silence till escalation — a sleeping agent learned
        the letter in poll minutes, and the pair worked only while the human pushed
        by hand. The postman new contract: push at once, memory beside, this same
        tick flush drives it into a free composer.
        """
        self.inbox("agy", 3)
        self.write_state(roles={"agy": {"last_seen": ago(60), "pane": "%1"}}, seen={"agy": 0})
        pilot = self.pilot()
        pilot.tick()
        wake = self.wake(pilot, "agy")
        self.assertTrue(wake, "an unread letter must leave a trace")
        self.assertIn("unread", wake["reason"])
        self.assertIn("unread", wake["text"])
        self.assertEqual(3, wake["unread_at_ask"], "how many exactly stand unread")
        self.assert_enqueued_for_push(pilot, "agy")

    def test_read_mail_is_not_reminded(self) -> None:
        self.inbox("agy", 3)
        self.write_state(roles={"agy": {"last_seen": ago(60), "pane": "%1"}}, seen={"agy": 3})
        pilot = self.pilot()
        pilot.tick()
        self.assertEqual("", pilot.pending("agy"))


class TestPaneAmbiguity(PilotCase):
    def test_roles_sharing_a_pane_are_never_touched(self) -> None:
        """Two roles on one pane means the roster lies. Never strike it: "waking claude"
        would leave for the agy window."""
        self.inbox("claude", 5)
        self.write_state(writer="agy", seen={},
                         roles={"agy": {"last_seen": ago(60), "pane": "%20"},
                                "claude": {"last_seen": ago(60), "pane": "%20"}})
        pilot = self.pilot()
        pilot.tick()
        self.assertEqual("", pilot.pending("claude"), "never poke a shared pane")
        self.assertIn("pilot_pane_shared", self.transcript_kinds())


class TestOutboxStaleness(PilotCase):
    """A deferred reminder must never outlive the question.

    Observed live: a reminder about an obligation closed near three hours before
    composing still lay in the queue 22 minutes more, delivered as fresh — the human
    took a question with no live matter left. The code cause: `flush_outbox` on ComposerBusy
    ran `continue` repeating forever, while the file dropped only on good delivery.
    No lifetime stood at all, and "surviving delivery" silently turned delivering
    the rotten.
    """

    def _queue(self, pilot, role: str, text: str, age_seconds: float = 0.0) -> Path:
        pilot.enqueue(role, text)
        path = pilot.outbox / f"{role}.txt"
        when = time.time() - age_seconds
        os.utime(path, (when, when))
        return path

    def test_fresh_nudge_is_delivered(self) -> None:
        p = self.pilot()
        path = self._queue(p, "agy", "a fresh reminder")
        state = self.write_state(roles={"agy": {"pane": "%1", "pid": os.getpid()}})
        orig_alive, orig_deliver = pairpilot.pane_alive, pairpilot.deliver
        orig_belongs = pairpilot.pane_belongs_to_project
        pairpilot.pane_alive = lambda tmux, pane: True
        pairpilot.pane_belongs_to_project = lambda tmux, pane, root: (True, "")
        pairpilot.deliver = lambda pane, text, wait_for_empty=0.0: {"delivered": True}
        try:
            p.tmux = "tmux"
            p.flush_outbox(state)
        finally:
            pairpilot.pane_alive, pairpilot.deliver = orig_alive, orig_deliver
            pairpilot.pane_belongs_to_project = orig_belongs
        self.assertFalse(path.exists(), "a fresh reminder delivers and drops")

    def test_stale_nudge_is_dropped_and_never_delivered(self) -> None:
        """A stale question delivers worse than never: delivering it reads worse."""
        p = self.pilot()
        p.outbox_ttl = 600.0
        path = self._queue(p, "agy", "a two-hour-old question", age_seconds=7200)
        state = self.write_state(roles={"agy": {"pane": "%1", "pid": os.getpid()}})
        calls = []
        orig_alive, orig_deliver = pairpilot.pane_alive, pairpilot.deliver
        orig_belongs = pairpilot.pane_belongs_to_project
        pairpilot.pane_alive = lambda tmux, pane: True
        pairpilot.pane_belongs_to_project = lambda tmux, pane, root: (True, "")
        pairpilot.deliver = lambda pane, text, wait_for_empty=0.0: calls.append(text) or {"delivered": True}
        try:
            p.tmux = "tmux"
            p.flush_outbox(state)
        finally:
            pairpilot.pane_alive, pairpilot.deliver = orig_alive, orig_deliver
            pairpilot.pane_belongs_to_project = orig_belongs
        self.assertEqual(calls, [], "the rotten never delivers even at a free composer")
        self.assertFalse(path.exists(), "the rotten drops from the queue")
        kinds = [e.get("kind") for e in p.store.read_transcript(limit=0)]
        self.assertIn("pilot_nudge_expired", kinds,
                      "a drop must show in the transcript, or it reads as delivered")

    def test_retained_submit_keeps_file_and_names_it(self) -> None:
        """Round 98: the composer kept the text — the file stays, the journal names it."""
        p = self.pilot()
        path = self._queue(p, "agy", "a hanging nudge")
        state = self.write_state(roles={"agy": {"pane": "%1", "pid": os.getpid()}})
        orig_alive, orig_deliver = pairpilot.pane_alive, pairpilot.deliver
        orig_belongs = pairpilot.pane_belongs_to_project
        pairpilot.pane_alive = lambda tmux, pane: True
        pairpilot.pane_belongs_to_project = lambda tmux, pane, root: (True, "")
        pairpilot.deliver = lambda pane, text, wait_for_empty=0.0: {
            "delivered": True, "submit_state": "retained"}
        try:
            p.tmux = "tmux"
            p.flush_outbox(state)
        finally:
            pairpilot.pane_alive, pairpilot.deliver = orig_alive, orig_deliver
            pairpilot.pane_belongs_to_project = orig_belongs
        self.assertTrue(path.exists(), "retained text stays queued for the mailbox")
        self.assertIn("pilot_deliver_unverified", self.transcript_kinds())

    def test_expired_nudge_falls_back_to_mailbox(self) -> None:
        """The rotten in the composer duplicates into the box, never vanishes.

        Measured 2026-10-06: two nudge_expired in a row, both agents alive, both
        got nothing. The pointer rotted in outbox while the agent worked with a
        busy composer. Mailbox never rots: the agent takes the letter over
        pair_read whenever it decides.
        """
        p = self.pilot()
        p.outbox_ttl = 600.0
        path = self._queue(p, "agy", "read pair_read", age_seconds=7200)
        state = self.write_state(roles={"agy": {"pane": "%1", "pid": os.getpid()}})
        orig_alive = pairpilot.pane_alive
        pairpilot.pane_alive = lambda tmux, pane: True
        try:
            p.tmux = "tmux"
            p.flush_outbox(state)
        finally:
            pairpilot.pane_alive = orig_alive
        inbox = list((self.state_dir / "mailbox" / "agy").glob("*.json"))
        self.assertEqual(1, len(inbox), "the pointer must lie in the box")
        body = json.loads(inbox[0].read_text(encoding="utf-8"))
        self.assertEqual("pairpilot", body["from"])
        self.assertIn("pair_read", body["text"])
        kinds = [e.get("kind") for e in p.store.read_transcript(limit=0)]
        self.assertIn("pilot_mailbox_fallback", kinds)

    def test_unread_nudge_goes_stale_when_mail_is_read(self) -> None:
        """F6 (round 87): an unread-pointer is dropped once its condition dies.

        Measured 2026-10-13: a pointer raised at unread=1 was delivered 62s
        later at unread=0. The check is by state (unread), not by text.
        Goes red if the nudge_still_true branch is removed: the file stays.
        """
        p = self.pilot()
        # 1 letter in the box, seen=0 -> unread=1: condition holds
        self.inbox("agy", 1)
        path = self._queue(p, "agy", "[pairpilot] У тебя 1 непрочитанных сообщений. Вызови pair_read")
        state = self.write_state(roles={"agy": {"pane": "%1", "pid": os.getpid()}}, seen={})
        self.assertTrue(p.nudge_still_true(state, "agy", p.pending("agy")),
                        "with unread mail the pointer still holds")
        # mail read: seen=1 -> unread=0, the condition is dead
        state = self.write_state(roles={"agy": {"pane": "%1", "pid": os.getpid()}}, seen={"agy": 1})
        p.drop_stale_outbox(state)
        self.assertFalse(path.exists(), "a stale pointer leaves the queue")
        self.assertIn("pilot_nudge_stale", self.transcript_kinds(),
                      "the drop is visible in the transcript")

    def test_unread_nudge_survives_while_mail_unread(self) -> None:
        """A pointer with a live condition survives — the guard spares fresh nudges."""
        p = self.pilot()
        self.inbox("agy", 2)
        path = self._queue(p, "agy", "[pairpilot] У тебя 2 непрочитанных сообщений. Вызови pair_read")
        state = self.write_state(roles={"agy": {"pane": "%1", "pid": os.getpid()}}, seen={})
        p.drop_stale_outbox(state)
        self.assertTrue(path.exists(), "a live pointer stays in the queue")

    def test_unread_nudge_en_pointer_goes_stale_when_mail_is_read(self) -> None:
        """EN twin of the F6 test: the bilingual matcher catches new pointers.

        Same commit as the matcher change (no window with a green gate and a
        one-sided matcher). Goes red if "unread" leaves the matcher.
        """
        p = self.pilot()
        self.inbox("agy", 1)
        path = self._queue(p, "agy", "[pairpilot] You have 1 unread messages. Call pair_read")
        state = self.write_state(roles={"agy": {"pane": "%1", "pid": os.getpid()}}, seen={})
        self.assertTrue(p.nudge_still_true(state, "agy", p.pending("agy")),
                        "with unread mail the EN pointer still holds")
        state = self.write_state(roles={"agy": {"pane": "%1", "pid": os.getpid()}}, seen={"agy": 1})
        p.drop_stale_outbox(state)
        self.assertFalse(path.exists(), "a stale EN pointer leaves the queue")

    def test_producer_text_still_matches_consumer_matcher(self) -> None:
        """Round-trip (F3 seq 1153): the consumer classifies the producer output.

        Uses the REAL queued text, not a literal: if unread_peers changes its
        wording and nudge_still_true does not follow (or vice versa), the stale
        pointer survives and this goes red. Independent of any RU literal, so
        translation cannot silently disarm it.
        """
        p = self.pilot()
        self.inbox("agy", 1)
        state = self.write_state(roles={"agy": {"pane": "%1", "pid": os.getpid()}}, seen={})
        p.unread_peers(state)
        produced = p.pending("agy")
        self.assertTrue(produced, "producer must queue a pointer")
        self.assertTrue(p.nudge_still_true(state, "agy", produced),
                        "live pointer holds while mail is unread")
        read_state = self.write_state(roles={"agy": {"pane": "%1", "pid": os.getpid()}},
                                      seen={"agy": 1})
        self.assertFalse(p.nudge_still_true(read_state, "agy", produced),
                         "producer text must die with its condition, or the queue never drops it")
    def test_reminder_producer_text_still_matches_consumer_matcher(self) -> None:
        """Round-trip on the obligation-reminder producer (critic seq 1219).

        The request-370 test pins a HAND-WRITTEN literal: it proves the matcher
        takes English, never that the PRODUCER wording is what the matcher takes.
        If pending_answers rewords again (query/item), the literal stays green while
        prod stops matching. So this takes the REAL queued text: with the obligation
        file open the producer wording must hold the condition, with it closed the
        same text must drop.
        """
        p = self.pilot()
        p.reply_grace = 0
        root = self.state_dir / "obligations"
        root.mkdir(parents=True, exist_ok=True)
        (root / "000370-q.json").write_text(json.dumps({
            "seq": 370, "from": "omp", "to": "agy",
            "opened_at": ago(120), "subject": "the linter question",
        }), encoding="utf-8")
        state = self.write_state(roles={"agy": {"pane": "%1", "pid": os.getpid()}})
        p.tick()
        produced = p.pending("agy")
        self.assertIn("370", produced,
                      "the producer must queue a reminder naming the request")
        self.assertTrue(p.nudge_still_true(state, "agy", produced),
                        "producer wording must hold while the obligation is open")
        (root / "000370-q.json").unlink()
        self.assertFalse(p.nudge_still_true(state, "agy", produced),
                         "the same producer text must drop past the close")

    def test_foreign_pane_blocks_delivery_not_heal(self) -> None:
        """F7 (round 87): delivery checks pane ownership against the project.

        Asymmetry: discovery checks (pane_belongs_to_project on the heal path),
        delivery did not. Stub belongs to refuse: deliver must not fire,
        the transcript gets pilot_pane_foreign. Stubbing deliver itself is
        banned — then the test would discriminate nothing.
        """
        p = self.pilot()
        path = self._queue(p, "agy", "a fresh reminder")
        state = self.write_state(roles={"agy": {"pane": "%9", "pid": os.getpid()}})
        calls = []
        orig_deliver = pairpilot.deliver
        orig_belongs = pairpilot.pane_belongs_to_project
        pairpilot.deliver = lambda pane, text, wait_for_empty=0.0: calls.append((pane, text)) or {"delivered": True}
        pairpilot.pane_belongs_to_project = lambda tmux, pane, root: (False, "foreign")
        orig_alive = pairpilot.pane_alive
        pairpilot.pane_alive = lambda tmux, pane: True
        try:
            p.tmux = "tmux"
            p.flush_outbox(state)
        finally:
            pairpilot.deliver = orig_deliver
            pairpilot.pane_belongs_to_project = orig_belongs
            pairpilot.pane_alive = orig_alive
        self.assertEqual(calls, [], "never print into a foreign pane")
        self.assertFalse(path.exists(), "a refusal leaves the queue instead of hanging")
        self.assertIn("pilot_pane_foreign", self.transcript_kinds(),
                      "the refusal is visible in the transcript, with reason")

    def test_dead_pane_refusal_keeps_the_file(self) -> None:
        """Verdict-87.3: (False, 'no such pane') is unproven, not foreign.

        A dead pane must not read pilot_pane_foreign and must not lose the
        file: it waits for the next tick like a busy composer does.
        """
        p = self.pilot()
        path = self._queue(p, "agy", "queued nudge")
        state = self.write_state(roles={"agy": {"pane": "%9", "pid": os.getpid()}})
        calls = []
        orig_deliver = pairpilot.deliver
        orig_belongs = pairpilot.pane_belongs_to_project
        orig_alive = pairpilot.pane_alive
        pairpilot.deliver = lambda pane, text, wait_for_empty=0.0: calls.append((pane, text)) or {"delivered": True}
        pairpilot.pane_belongs_to_project = lambda tmux, pane, root: (False, "no such pane")
        pairpilot.pane_alive = lambda tmux, pane: False
        try:
            p.tmux = "tmux"
            p.flush_outbox(state)
        finally:
            pairpilot.deliver = orig_deliver
            pairpilot.pane_belongs_to_project = orig_belongs
            pairpilot.pane_alive = orig_alive
        self.assertEqual(calls, [], "nothing is printed while ownership is unproven")
        self.assertTrue(path.exists(), "the file survives for the next tick")
        kinds = self.transcript_kinds()
        self.assertIn("pilot_pane_unproven", kinds)
        self.assertNotIn("pilot_pane_foreign", kinds,
                         "a dead pane must not be logged as a foreign project")

    def test_failed_check_keeps_file_without_foreign_mark(self) -> None:
        """Verdict-87 follow-up: a throwing check is unproven, even on a live pane.

        pane_alive True + belongs raising must keep the file and must not
        write pilot_pane_foreign: 'could not check' is not 'proven foreign'.
        """
        p = self.pilot()
        path = self._queue(p, "agy", "queued nudge")
        state = self.write_state(roles={"agy": {"pane": "%1", "pid": os.getpid()}})

        def boom(tmux, pane, root):
            raise RuntimeError("tmux transient failure")

        calls = []
        orig_deliver = pairpilot.deliver
        orig_belongs = pairpilot.pane_belongs_to_project
        orig_alive = pairpilot.pane_alive
        pairpilot.deliver = lambda pane, text, wait_for_empty=0.0: calls.append((pane, text)) or {"delivered": True}
        pairpilot.pane_belongs_to_project = boom
        pairpilot.pane_alive = lambda tmux, pane: True
        try:
            p.tmux = "tmux"
            p.flush_outbox(state)
            p.flush_outbox(state)
        finally:
            pairpilot.deliver = orig_deliver
            pairpilot.pane_belongs_to_project = orig_belongs
            pairpilot.pane_alive = orig_alive
        self.assertEqual(calls, [], "nothing printed while the check fails")
        self.assertTrue(path.exists(), "the file survives a transient check failure")
        kinds = self.transcript_kinds()
        self.assertNotIn("pilot_pane_foreign", kinds,
                         "an unavailable check must not log proven foreignness")
        self.assertEqual(1, kinds.count("pilot_pane_unproven"),
                         "one queued pointer records unproven once, not per tick")

    def test_own_pane_delivery_unaffected_by_belongs_gate(self) -> None:
        """An owned pane delivers as before — the gate spares the normal path."""
        p = self.pilot()
        path = self._queue(p, "agy", "a fresh reminder")
        state = self.write_state(roles={"agy": {"pane": "%1", "pid": os.getpid()}})
        calls = []
        orig_deliver = pairpilot.deliver
        orig_belongs = pairpilot.pane_belongs_to_project
        pairpilot.deliver = lambda pane, text, wait_for_empty=0.0: calls.append((pane, text)) or {"delivered": True}
        pairpilot.pane_belongs_to_project = lambda tmux, pane, root: (True, "")
        orig_alive2 = pairpilot.pane_alive
        pairpilot.pane_alive = lambda tmux, pane: True
        try:
            p.tmux = "tmux"
            p.flush_outbox(state)
        finally:
            pairpilot.deliver = orig_deliver
            pairpilot.pane_belongs_to_project = orig_belongs
            pairpilot.pane_alive = orig_alive2
        self.assertEqual(1, len(calls), "an owned pane is delivered")
        self.assertFalse(path.exists(), "delivered mail leaves the queue")

    def test_a_busy_composer_does_not_expire_a_fresh_nudge(self) -> None:
        """Surviving delivery stays: while the composer is busy, waiting is fit."""
        p = self.pilot()
        p.outbox_ttl = 600.0
        path = self._queue(p, "agy", "we wait", age_seconds=60)
        state = self.write_state(roles={"agy": {"pane": "%1", "pid": os.getpid()}})

        def busy(pane, text, wait_for_empty=0.0):
            raise pairpilot.ComposerBusy("busy")

        orig_alive, orig_deliver = pairpilot.pane_alive, pairpilot.deliver
        orig_belongs = pairpilot.pane_belongs_to_project
        pairpilot.pane_alive = lambda tmux, pane: True
        pairpilot.pane_belongs_to_project = lambda tmux, pane, root: (True, "")
        pairpilot.deliver = busy
        try:
            p.tmux = "tmux"
            p.flush_outbox(state)
        finally:
            pairpilot.pane_alive, pairpilot.deliver = orig_alive, orig_deliver
            pairpilot.pane_belongs_to_project = orig_belongs
        self.assertTrue(path.exists(), "a fresh reminder survives a busy composer")


class TestReport(PilotCase):
    def test_closed_round_produces_a_report(self) -> None:
        self.write_state(phase="done", closed_at=ago(60), round=3, writer="agy",
                         roles={"agy": {"last_seen": ago(60), "pane": "%1"}})
        (self.state_dir / "transcript.jsonl").write_text(
            json.dumps({"kind": "deliver", "role": "agy"}) + "\n"
            + json.dumps({"kind": "deliver_busy", "role": "opencode"}) + "\n",
            encoding="utf-8")
        pilot = self.pilot()
        pilot.tick()
        report = self.state_dir / "REPORT.md"
        self.assertTrue(report.exists(), "a closed round must leave a report")
        text = report.read_text(encoding="utf-8")
        self.assertIn("test", text)
        self.assertIn("deliver_busy", text)
        self.assertIn("does NOT prove", text)
        self.assertIn("pilot_report", self.transcript_kinds())

    def test_open_round_writes_nothing(self) -> None:
        self.write_state(phase="verify", roles={"agy": {"last_seen": ago(10), "pane": "%1"}})
        pilot = self.pilot()
        pilot.tick()
        self.assertFalse((self.state_dir / "REPORT.md").exists())

    def test_report_is_written_once(self) -> None:
        self.write_state(phase="done", closed_at=ago(60), round=3,
                         roles={"agy": {"last_seen": ago(60), "pane": "%1"}})
        pilot = self.pilot()
        pilot.tick()
        first = (self.state_dir / "REPORT.md").stat().st_mtime_ns
        pilot.tick()
        self.assertEqual(first, (self.state_dir / "REPORT.md").stat().st_mtime_ns,
                         "the report never rewrites every pass")


class TestSafety(PilotCase):
    def test_pilot_never_touches_write_authority(self) -> None:
        """The edge the supervisor supervises for: it holds no right to grant or take
        authority, even when an agent stays silent."""
        self.write_state(writer="agy", roles={"agy": {"last_seen": ago(9999), "pane": "%1"}})
        pilot = self.pilot()
        pilot.tick()
        state = json.loads((self.state_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual("agy", state.get("writer"))
        self.assertNotIn("assign", {k for k in dir(pairpilot.Pilot) if "assign" in k})

    def test_pilot_never_moves_the_phase(self) -> None:
        self.write_state(writer="agy", phase="verify",
                         roles={"agy": {"last_seen": ago(9999), "pane": "%1"}})
        pilot = self.pilot()
        pilot.tick()
        state = json.loads((self.state_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual("verify", state.get("phase"))

    def test_silence_threshold_is_honoured(self) -> None:
        self.write_state(writer="agy", roles={"agy": {"last_seen": ago(400), "pane": "%1"}})
        tolerant = self.pilot(silence=600)
        tolerant.tick()
        self.assertEqual("", tolerant.pending("agy"), "400s of silence at a 600s threshold — the norm")

    def test_short_silence_threshold_wakes(self) -> None:
        self.write_state(writer="agy", roles={"agy": {"last_seen": ago(400), "pane": "%1"}})
        strict = self.pilot(silence=100)
        strict.tick()
        self.assertTrue(self.wake(strict, "agy"),
                        "400s of silence at a 100s threshold — a defect")


class TestTmuxPaths(PilotCase):
    """Paths that call tmux. They need a stub, or they check only live — exactly where
    append_transcript fell with a foreign role=."""

    def fake_tmux(self, alive=True, prompt=None, verdict="allow"):
        pilot = self.pilot()
        pilot.tmux = "/usr/bin/true"
        pairpilot.pane_alive = lambda tmux, pane: alive
        pairpilot.resolve_permission_prompt = (
            lambda tmux, pane, allow=None: {"approved": verdict == "allow",
                                            "verdict": verdict,
                                            "prompt": prompt or "",
                                            "why": "on the allowlist" if verdict == "allow" else "off the allowlist",
                                            "matched": None})
        return pilot

    def tearDown(self) -> None:
        pairpilot.pane_alive = self._real_alive
        pairpilot.resolve_permission_prompt = self._real_prompt
        pairpilot.subprocess = self._real_subprocess
        super().tearDown()

    def setUp(self) -> None:
        self._real_alive = getattr(pairpilot, "pane_alive", None)
        self._real_prompt = getattr(pairpilot, "resolve_permission_prompt", None)
        self._real_subprocess = getattr(pairpilot, "subprocess", None)
        super().setUp()

    def test_missing_pane_is_recorded_without_crashing(self) -> None:
        self.write_state(roles={"codex": {"last_seen": ago(10)}})
        pilot = self.fake_tmux()
        pilot.tick()
        self.assertIn("pilot_pane_missing", self.transcript_kinds())

    def test_live_pid_without_pane_heals_the_roster(self) -> None:
        """No pane in the roster, but the role process lives sitting in a pane: heal the
        record, never bury the role. Or deliver goes blind at a live neighbour."""
        import os
        self.write_state(roles={"codex": {"last_seen": ago(10), "pid": os.getpid()}})
        pilot = self.fake_tmux()
        real_find = pairpilot.find_pane_of_pid
        pairpilot.find_pane_of_pid = lambda tmux, pid: "%7"
        try:
            pilot.tick()
        finally:
            pairpilot.find_pane_of_pid = real_find
        self.assertIn("pilot_pane_healed", self.transcript_kinds())
        self.assertEqual(self.pilot().store.read()["roles"]["codex"]["pane"], "%7")

    def test_live_project_pane_heals_roster_without_pid(self) -> None:
        """A dead/empty pid with a live free pane in the project: heal the record by cwd,
        never bury the role (P1, critic seq 381)."""
        self.write_state(roles={"codex": {"last_seen": ago(10), "pid": 1}})
        pilot = self.fake_tmux()
        real_find = pairpilot.find_pane_of_pid
        real_belongs = pairpilot.pane_belongs_to_project
        real_rolepid = pairpilot.pane_role_pid
        real_run = pairpilot.subprocess.run
        pairpilot.find_pane_of_pid = lambda tmux, pid: ""
        pairpilot.pane_belongs_to_project = (
            lambda tmux, pane, root: (pane == "%11", ""))
        pairpilot.pane_role_pid = lambda tmux, pane, role: 4242 if pane == "%11" else 0
        pairpilot.subprocess.run = lambda *a, **k: type("R", (), {
            "stdout": "%9\n%11\n", "returncode": 0})()
        pilot.pane_key = lambda pane: str(pane)
        try:
            pilot.tick()
        finally:
            pairpilot.find_pane_of_pid = real_find
            pairpilot.pane_belongs_to_project = real_belongs
            pairpilot.pane_role_pid = real_rolepid
            pairpilot.subprocess.run = real_run
        self.assertIn("pilot_pane_healed", self.transcript_kinds())
        self.assertEqual(self.pilot().store.read()["roles"]["codex"]["pane"], "%11")

    def test_foreign_pane_does_not_heal_the_roster(self) -> None:
        """A foreign pane never heals the roster: belonging unproven — the role stays
        paneless, fail-closed."""
        self.write_state(roles={"codex": {"last_seen": ago(10), "pid": 1}})
        pilot = self.fake_tmux()
        real_find = pairpilot.find_pane_of_pid
        real_belongs = pairpilot.pane_belongs_to_project
        real_run = pairpilot.subprocess.run
        pairpilot.find_pane_of_pid = lambda tmux, pid: ""
        pairpilot.pane_belongs_to_project = lambda tmux, pane, root: (False, "foreign")
        pairpilot.subprocess.run = lambda *a, **k: type("R", (), {"stdout": "%9\n", "returncode": 0})()
        pilot.pane_key = lambda pane: str(pane)
        try:
            pilot.tick()
        finally:
            pairpilot.find_pane_of_pid = real_find
            pairpilot.pane_belongs_to_project = real_belongs
            pairpilot.subprocess.run = real_run
        self.assertIn("pilot_pane_missing", self.transcript_kinds())
        self.assertNotIn("pane", self.pilot().store.read()["roles"]["codex"])

    def test_human_shell_is_not_adopted_without_role_server(self) -> None:
        """A project pane with no role server.py (a human shell) never adopts: cwd is the
        second factor, the first — the live role process (round-33 verdict)."""
        self.write_state(roles={"codex": {"last_seen": ago(10), "pid": 1}})
        pilot = self.fake_tmux()
        real_find = pairpilot.find_pane_of_pid
        real_belongs = pairpilot.pane_belongs_to_project
        real_rolepid = pairpilot.pane_role_pid
        real_run = pairpilot.subprocess.run
        pairpilot.find_pane_of_pid = lambda tmux, pid: ""
        pairpilot.pane_belongs_to_project = lambda tmux, pane, root: (True, "")
        pairpilot.pane_role_pid = lambda tmux, pane, role: 0
        pairpilot.subprocess.run = lambda *a, **k: type("R", (), {"stdout": "%9\n", "returncode": 0})()
        pilot.pane_key = lambda pane: str(pane)
        try:
            pilot.tick()
        finally:
            pairpilot.find_pane_of_pid = real_find
            pairpilot.pane_belongs_to_project = real_belongs
            pairpilot.pane_role_pid = real_rolepid
            pairpilot.subprocess.run = real_run
        self.assertIn("pilot_pane_missing", self.transcript_kinds())
        self.assertNotIn("pane", self.pilot().store.read()["roles"]["codex"])

    def test_provisional_pane_is_preferred_but_rechecked(self) -> None:
        """The pane_provisional lifecycle: the server saw the pane, the pilot ranks it
        first, but rechecks the factors — with no server.py never adopts even it
        (round-33 verdict, seq 392)."""
        self.write_state(roles={"codex": {"last_seen": ago(10), "pid": 1,
                                          "pane_provisional": "%11"}})
        pilot = self.fake_tmux()
        real_belongs = pairpilot.pane_belongs_to_project
        real_rolepid = pairpilot.pane_role_pid
        real_run = pairpilot.subprocess.run
        pairpilot.pane_belongs_to_project = lambda tmux, pane, root: (True, "")
        pairpilot.pane_role_pid = lambda tmux, pane, role: 4242
        pairpilot.subprocess.run = lambda *a, **k: type("R", (), {
            "stdout": "%9\n%11\n", "returncode": 0})()
        pilot.pane_key = lambda pane: str(pane)
        try:
            found = pilot.live_project_panes(self.pilot().store.read(), "codex")
        finally:
            pairpilot.pane_belongs_to_project = real_belongs
            pairpilot.pane_role_pid = real_rolepid
            pairpilot.subprocess.run = real_run
        self.assertEqual("%11", found[0], "provisional ranks first in the queue")

    def test_wake_placement_rings_the_pane_without_typing(self) -> None:
        """A wake placement rings the pane by display-message, never types into the
        composer: the ring shows, the input line stays whole."""
        self.write_state(roles={"codex": {"last_seen": ago(10), "pane": "%9"}})
        pilot = self.fake_tmux()
        calls = []
        real_run = pairpilot.subprocess.run
        pairpilot.subprocess.run = lambda *a, **k: calls.append(a[0]) or type("R", (), {
            "stdout": "", "returncode": 0})()
        try:
            pilot.mark_wake(self.pilot().store.read(), "codex", "message", "text")
        finally:
            pairpilot.subprocess.run = real_run
        self.assertIn("pilot_ring", self.transcript_kinds())
        self.assertTrue(any("display-message" in c for c in calls),
                        "the ring goes by display-message, not send-keys")
        self.assertFalse(any("send-keys" in c for c in calls),
                         "typing into the composer is banned")

    def test_ring_is_throttled_to_five_minutes(self) -> None:
        """A second ring in a row is silence: never ringing more often than 5 min."""
        self.write_state(roles={"codex": {"last_seen": ago(10), "pane": "%9"}})
        pilot = self.fake_tmux()
        real_run = pairpilot.subprocess.run
        pairpilot.subprocess.run = lambda *a, **k: type("R", (), {
            "stdout": "", "returncode": 0})()
        try:
            state = self.pilot().store.read()
            pilot.mark_wake(state, "codex", "message", "text")
            before = self.transcript_kinds().count("pilot_ring")
            pilot.mark_wake(self.pilot().store.read(), "codex", "message", "text")
            after = self.transcript_kinds().count("pilot_ring")
        finally:
            pairpilot.subprocess.run = real_run
        self.assertEqual(before, after, "a repeat ring inside 5 min is banned")

    def test_dead_pane_is_recorded_once(self) -> None:
        self.write_state(roles={"agy": {"last_seen": ago(10), "pane": "%9"}})
        pilot = self.fake_tmux(alive=False)
        pilot.tick()
        pilot.tick()
        self.assertEqual(1, self.transcript_kinds().count("pilot_pane_dead"),
                         "a dead pane speaks once, never every 15 seconds")

    def test_role_without_wake_adapter_is_named_not_silent(self) -> None:
        """No adapter means a journal record, not silence (pi stayed silent 3 hours)."""
        self.write_state(roles={"pi": {"last_seen": ago(10), "pane": "%9"}})
        self.fake_tmux().mark_wake(self.pilot().store.read(), "pi", "message", "text")
        self.assertIn("pilot_wake_no_adapter", self.transcript_kinds())

    def test_permission_prompt_is_confirmed_and_logged(self) -> None:
        self.write_state(roles={"agy": {"last_seen": ago(10), "pane": "%9"}})
        pilot = self.fake_tmux(prompt="Allow Read of server.py?")
        pilot.tick()
        self.assertIn("pilot_unblock", self.transcript_kinds())

    def test_refused_permission_is_recorded_not_pressed(self) -> None:
        """A refusal is a result. Silently skipping "y" is banned: the human must know the
        pane stands and why he must step to it himself."""
        self.write_state(roles={"agy": {"last_seen": ago(10), "pane": "%9"}})
        pilot = self.fake_tmux(prompt="Allow execution of bash? rm -rf /", verdict="refuse")
        pilot.tick()
        self.assertIn("pilot_unblock_refused", self.transcript_kinds())
        self.assertNotIn("pilot_unblock", self.transcript_kinds())

    def test_observe_mode_never_confirms_a_prompt(self) -> None:
        self.write_state(autonomy="observe", roles={"agy": {"last_seen": ago(10), "pane": "%9"}})
        pilot = self.fake_tmux(prompt="Allow Read of server.py?")
        pilot.tick()
        self.assertNotIn("pilot_unblock", self.transcript_kinds(),
                         "in observe the human drives, the supervisor only speaks")

    def test_live_pane_produces_no_panic(self) -> None:
        self.write_state(writer="agy", roles={"agy": {"last_seen": ago(10), "pane": "%9"}})
        pilot = self.fake_tmux()
        pilot.tick()
        self.assertNotIn("pilot_pane_dead", self.transcript_kinds())


class TestRestart(PilotCase):
    """Restarting a fallen agent. It fixes exactly the night breakage where pid reads
    GONE while the roster record lives for hours."""

    DEAD_PID = 999999

    def setUp(self) -> None:
        self._real_subprocess = getattr(pairpilot, "subprocess", None)
        self.calls: list[list[str]] = []
        pairpilot.subprocess = _FakeSubprocess(self.calls)
        # Module stubs restore here, not per test.
        # Proven no theory: a pane_pid stub from one test outlived into the next ones,
        # randomizing their result — two tests fell till the stub returned past leaking.
        # A test that depends on launch order checks not its own behaviour.
        # checks not its own behaviour.
        self._real_pane_alive = pairpilot.pane_alive
        self._real_pane_role_pid = getattr(pairpilot, "pane_role_pid", None)
        pairpilot.pane_role_pid = lambda tmux, pane, role: None
        # deliver types into a live composer over tmux send-keys: with no mock the suite
        # spammed a real pane %7 (round 42, agy complaint).
        self._real_deliver = pairpilot.deliver
        pairpilot.deliver = lambda pane, text, wait_for_empty=0.0: {"delivered": True}
        super().setUp()

    def tearDown(self) -> None:
        pairpilot.subprocess = self._real_subprocess
        pairpilot.pane_alive = self._real_pane_alive
        pairpilot.deliver = self._real_deliver
        if self._real_pane_role_pid is not None:
            pairpilot.pane_role_pid = self._real_pane_role_pid
        super().tearDown()

    def dead_writer_state(self, **extra) -> None:
        self.write_state(writer="agy", round=3,
                         roles={"agy": {"last_seen": ago(600), "pid": self.DEAD_PID, "pane": "%999999"}},
                         **extra)

    def pilot_with_launch(self, command: str = "agy --resume"):
        pilot = self.pilot()
        pilot.tmux = "/usr/bin/tmux"
        pairpilot.pane_alive = lambda tmux, pane: True
        pilot._explicit_launch = {"agy": command} if command else {}
        return pilot

    def respawns(self, role: str | None = None) -> list[list[str]]:
        """`respawn-pane` calls, and only they.

        The tests below claim meaning — "role restarted" or "role NOT restarted"
        — never a command list. The difference is no taste: `pane_alive` and
        `pane_identity` reach tmux themselves, so the supervisor may well need one
        more `display-message` where none stood before. A test demanding
        `self.calls == []` would fall on any such call, fixable only one way — by
        never checking. So restarts filter here, while other-call count and order
        never assert at all.
        """
        out = [call for call in self.calls if "respawn-pane" in call]
        if role is not None:
            out = [call for call in out if role in " ".join(call)]
        return out

    def assertNoRestart(self, why: str) -> None:
        self.assertEqual([], self.respawns(), why)

    def test_the_restart_never_kills_whats_in_the_pane(self) -> None:
        """`-k` stood the only thing allowing to kill the human process.

        Without it tmux refuses restarting a live pane, so a busy pane — a shell or
        a debugger the human opened past the agent fall — stays untouched, and the
        refusal lands in the transcript as a refusal.
        """
        self.dead_writer_state()
        pilot = self.pilot_with_launch()
        pilot.tick()
        sent = self.respawns()
        self.assertTrue(sent)
        for call in sent:
            self.assertNotIn("-k", call, "a restart must never kill the pane content")

    def test_a_dead_role_does_not_make_a_pane_shared(self) -> None:
        """A live defect: claude and codex stood in the roster with the agy pane, and
        `skip` held all three — the supervisor stopped serving a live neighbour
        over two dead records."""
        self.write_state(writer="agy", round=3, roles={
            "agy": {"last_seen": ago(10), "pid": os.getpid(), "pane": "%7"},
            "claude": {"last_seen": ago(9000), "pid": 999999, "pane": "%7"},
        })
        pilot = self.pilot()
        pilot.tmux = "/usr/bin/tmux"
        pilot.tick()
        self.assertNotIn("agy", pilot.skip, "a live neighbour must never fall under skip")
        self.assertNotIn("pilot_pane_shared", self.transcript_kinds())
        self.assertIn("pilot_stale_role", self.transcript_kinds())

    def test_a_stale_role_is_reported_once_not_every_tick(self) -> None:
        self.write_state(writer="agy", roles={
            "agy": {"last_seen": ago(10), "pid": os.getpid(), "pane": "%7"},
            "codex": {"last_seen": ago(9000), "pid": 999999, "pane": "%7"},
        })
        pilot = self.pilot()
        pilot.tick()
        pilot.tick()
        pilot.tick()
        stale = [k for k in self.transcript_kinds() if k == "pilot_stale_role"]
        self.assertEqual(1, len(stale), "a dead role speaks once, never every tick")

    def test_dead_writer_is_respawned(self) -> None:
        self.dead_writer_state()
        pilot = self.pilot_with_launch()
        pilot.tick()
        # What asserts is the restart fact in the right pane with the right command, not
        # which call number it was: `self.calls[0]` claims order, breaking on any edit
        # in neighbouring code.
        self.assertEqual([["/usr/bin/tmux", "respawn-pane", "-t", "%999999", "agy --resume"]],
                         self.respawns(),
                         "a fallen writer restarts in its own pane")
        self.assertIn("pilot_restart", self.transcript_kinds())

    def test_restart_happens_once_per_dead_pid(self) -> None:
        self.dead_writer_state()
        pilot = self.pilot_with_launch()
        pilot.tick()
        pilot.tick()
        self.assertEqual(1, len(self.respawns()),
                         "one dead process means one restart, never every 15 seconds")

    def test_dying_on_start_stops_being_restarted_forever(self) -> None:
        """A hole found on review.

        A pid key saves from restarting the same fallen process twice — and exactly
        so never saves from another: with a wrong launch command the process dies
        with a NEW pid, the key reads new every time, and at a 15-second interval
        that is an endless loop. A windowed ceiling never depends on pid, so exactly
        it stops the loop.
        """
        pilot = self.pilot_with_launch()
        for attempt in range(6):
            # every failed attempt gives a new pid, as it would live
            self.write_state(writer="agy", round=3,
                             roles={"agy": {"last_seen": ago(600), "pid": 999000 + attempt,
                                            "pane": "%7"}})
            pilot.tick()
        respawns = self.respawns()
        self.assertEqual(pilot.restart_limit, len(respawns),
                         f"restarts must number no more than {pilot.restart_limit}, "
                         f"but they number {len(respawns)}")
        self.assertIn("pilot_restart_throttled", self.transcript_kinds())

    def test_restart_budget_expires_with_the_window(self) -> None:
        """A ceiling must never turn permanent: or a network drop would take the pair
        down for good."""
        pilot = self.pilot_with_launch()
        pilot.restart_window = 60.0
        for attempt in range(pilot.restart_limit + 2):
            self.write_state(writer="agy", round=3,
                             roles={"agy": {"last_seen": ago(600), "pid": 998000 + attempt,
                                            "pane": "%7"}})
            pilot.tick()
        self.assertEqual(pilot.restart_limit, len(self.respawns()))
        # The window expired: attempts outside the window must never count.
        pilot.restarts_file("agy").write_text(
            " ".join(str(int(time.time() - 3600)) for _ in range(pilot.restart_limit)),
            encoding="utf-8")
        self.write_state(writer="agy", round=3,
                         roles={"agy": {"last_seen": ago(600), "pid": 998111, "pane": "%7"}})
        pilot.tick()
        self.assertEqual(pilot.restart_limit + 1,
                         len(self.respawns()))
    def test_launch_profile_added_after_start_is_picked_up(self) -> None:
        """A profile added past the pilot start must pick up.

        The 2026-10-06 regression: the 05.10 pilot never knew the `pi` command and
        wrote `pilot_restart_impossible`, though `cli-profiles.json` already held
        the profile. The command map froze in `__init__` refreshing on no pilot
        restart.
        """
        self.dead_writer_state()
        pilot = self.pilot_with_launch(command="")
        pilot.tmux = "/usr/bin/tmux"
        pairpilot.pane_alive = lambda tmux, pane: True
        pilot.tick()
        self.assertNoRestart("no profile means no restart")
        self.assertIn("pilot_restart_impossible", self.transcript_kinds())
        pilot._explicit_launch["agy"] = "agy --resume"
        pilot.tick()
        self.assertEqual(1, len(self.respawns()),
                         "a command appearing past start means the restart must fire")

    def test_budget_is_per_role_not_global(self) -> None:
        pilot = self.pilot_with_launch()
        pilot._explicit_launch["claude"] = "claude"
        for _ in range(pilot.restart_limit + 2):
            self.write_state(writer="agy", round=3,
                             roles={"agy": {"last_seen": ago(600), "pid": self.DEAD_PID,
                                            "pane": "%7"}})
            pilot.tick()
        self.write_state(writer="agy", round=3,
                         roles={"agy": {"last_seen": ago(600), "pid": self.DEAD_PID, "pane": "%7"},
                                "claude": {"last_seen": ago(600), "pid": self.DEAD_PID, "pane": "%8"}})
        pilot.tick()
        self.assertTrue(self.respawns("claude"),
                        "another agent owns its own budget")

    def test_live_process_is_never_respawned(self) -> None:
        """Silence heals by reminder, death by restart. Killing one merely busy working
        means taking his context away."""
        self.write_state(writer="agy",
                         roles={"agy": {"last_seen": ago(10), "pid": os.getpid(), "pane": "%7"}})
        pilot = self.pilot_with_launch()
        pilot.tick()
        self.assertNoRestart("a live process never restarts")
        self.assertNotIn("pilot_restart", self.transcript_kinds())

    def test_a_live_process_in_the_pane_is_not_respawned(self) -> None:
        """The night main incident: 21 tries restarting a pane where the agent is ALIVE.

        The role restarted alone, or the pid merely changed, while the roster record
        kept the rotten one. The pane lives, the agent sits in it — and `respawn-pane`
        with `-k` would kill him with the whole round context.

        Exactly the incident checks: NO restart, while the record pid updates to the
        truly live one. No tmux-call order asserts here.
        """
        self.dead_writer_state()
        pilot = self.pilot_with_launch()
        # A live pid, and NOT our own: the supervisor launched from the role pane writes
        # no pid into its record, and stubbing os.getpid() would check the wrong thing.
        live = os.getppid()
        pairpilot.pane_role_pid = lambda tmux, pane, role: live
        pilot.tick()
        self.assertNoRestart("a live process in the pane — restarting would kill it")
        self.assertEqual(live, self.read_state()["roles"]["agy"]["pid"],
                         "the record pid must become the truly live one")
        self.assertIn("pilot_pid_stale_pane_alive", self.transcript_kinds())
        entry = [e for e in self.transcript_entries()
                 if e["kind"] == "pilot_pid_stale_pane_alive"][-1]
        self.assertEqual(live, entry["live_pid"])
        self.assertEqual(self.DEAD_PID, entry["pid"],
                         "the reason must name the rotten pid, not the current one")

    def test_a_dead_process_in_the_pane_is_still_respawned(self) -> None:
        """The flip side: a live PANE never means a live AGENT.

        Had `pane_pid` run on "pane busy means all fine", an agent fall at a live
        window would stay unnoticed — exactly the case the restart exists for. So
        the check mirrors the prior one, standing only together with it.
        """
        self.dead_writer_state()
        pilot = self.pilot_with_launch()
        pairpilot.pane_role_pid = lambda tmux, pane, role: self.DEAD_PID
        pilot.tick()
        self.assertEqual([["/usr/bin/tmux", "respawn-pane", "-t", "%999999", "agy --resume"]],
                         self.respawns(),
                         "a dead pid in the pane means the role rises")
        self.assertNotIn("pilot_pid_stale_pane_alive", self.transcript_kinds())

    def test_an_unprovable_pane_pid_does_not_block_the_restart(self) -> None:
        """"Unknown" must never read as "busy".

        Had a zero from `pane_pid` count as "pane busy", past any tmux error the
        supervisor would stop restarting fallen roles forever — silent, with no
        record. A refusal must stay loud and useful.
        """
        self.dead_writer_state()
        pilot = self.pilot_with_launch()
        pairpilot.pane_role_pid = lambda tmux, pane, role: 0
        pilot.tick()
        self.assertEqual(1, len(self.respawns()),
                         "unproven anyone sits in the pane — the restart never banned")
        self.assertEqual(self.DEAD_PID, self.read_state()["roles"]["agy"]["pid"],
                         "a record with no live pid never invents one")

    def test_a_squatters_pid_never_lands_in_the_role_record(self) -> None:
        """A pane busy with a FOREIGN role — holding a live process.

        This checks ORDER, not guard presence. Asking `pane_pid` before the
        "pane busy with another role" check returns the NEIGHBOUR live pid, and
        we would write it into this role record. Two records on one process — and
        the roster never sorts who sits where.

        Mutating this code: returning `pane_occupant` before `pane_occupied_by_other`
        reddens the test. Written to catch exactly that.
        """
        self.write_state(writer="agy", round=3, roles={
            "agy": {"last_seen": ago(600), "pid": self.DEAD_PID, "pane": "%7"},
            "claude": {"last_seen": ago(30), "pid": os.getpid(), "pane": "%7"},
        })
        pilot = self.pilot_with_launch()
        pilot.pane_key = lambda pane: "%7"
        # Someone truly lives in the pane — or the check would stand empty.
        pairpilot.pane_role_pid = lambda tmux, pane, role: os.getppid()
        pilot.tick()
        self.assertNoRestart("a pane busy with a live role stays untouched")
        self.assertEqual(self.DEAD_PID, self.read_state()["roles"]["agy"]["pid"],
                         "the neighbour pid must never land in this role record")
        self.assertNotIn("pilot_pid_stale_pane_alive", self.transcript_kinds(),
                         "a busy pane is a refusal, not a pid update")
        self.assertIn("pilot_restart_impossible", self.transcript_kinds())

    def test_a_live_shell_in_a_dead_roles_pane_is_not_mistaken_for_the_role(self) -> None:
        """A breakage my own tests missed.

        A fallen agent leaves a LIVE shell in the pane — so the terminal works. Deriving
        "role alive" from "a process sits in the pane" reads the fallen role alive
        always, cancelling the restart whole. Exactly the case `restart_agent` exists
        for, so a pane-based liveness fix both healed the restart and broke it.

        Measured on this machine: `#{pane_pid}` in a pane with a working agent is
        bash, not the agent. So the role server asks, not the pane process.

        The pid source stubs here as it behaves live: the pane holds a live shell,
        no role process in it. A restart expects.
        """
        self.dead_writer_state()
        pilot = self.pilot_with_launch()
        # A live foreign process in the pane — exactly what survives an agent.
        pairpilot.pane_role_pid = lambda tmux, pane, role: 0
        pilot.own_pane = None
        pilot.tick()
        self.assertEqual(1, len(self.respawns()),
                         "a live shell in the pane never proves the role alive — "
                         "or a fallen role would never restart")

    def test_a_stale_pid_is_refreshed_once_not_every_tick(self) -> None:
        """An update is one event, never a transcript line every 15s."""
        self.dead_writer_state()
        pilot = self.pilot_with_launch()
        pairpilot.pane_role_pid = lambda tmux, pane, role: os.getppid()
        pilot.tick()
        pilot.tick()
        pilot.tick()
        stale = [k for k in self.transcript_kinds() if k == "pilot_pid_stale_pane_alive"]
        self.assertEqual(1, len(stale), "an aged pid speaks once")

    def test_restart_needs_a_known_command(self) -> None:
        """No command means no restart with a loud record of it. An invented command
        reads worse than a missing one: the pane wakes and falls at once."""
        self.dead_writer_state()
        pilot = self.pilot_with_launch(command="")
        pilot.tick()
        self.assertNoRestart("no launch command means no restart")
        self.assertIn("pilot_restart_impossible", self.transcript_kinds())

    def test_dead_process_without_a_pane_is_left_to_the_human(self) -> None:
        """A window the human closed never rises silent: no hang, a decision."""
        self.write_state(writer="agy",
                         roles={"agy": {"last_seen": ago(600), "pid": self.DEAD_PID, "pane": "%7"}})
        pilot = self.pilot_with_launch()
        pairpilot.pane_alive = lambda tmux, pane: False
        pilot.tick()
        self.assertNoRestart("a window the human closed never rises silent")
        self.assertIn("pilot_restart_impossible", self.transcript_kinds())

    def test_observe_mode_does_not_restart(self) -> None:
        self.dead_writer_state(autonomy="observe")
        pilot = self.pilot_with_launch()
        pilot.tick()
        self.assertNoRestart("in observe a restart is an action, not a message")

    def test_own_pane_is_never_respawned(self) -> None:
        """The fifth restart brake: the supervisor own pane.

        `respawn-pane -k` kills the pane for good. On the human pane that holds his
        input and the supervisor itself: restarting the role would kill the operator
        who launched it. The other paths (reminder, unblock) spare this pane, and
        without the check the restart would stand the single exception to the rule
        written everywhere in words.
        """
        self.dead_writer_state()
        pilot = self.pilot_with_launch()
        pilot.own_pane = "%999999"
        pilot.tick()
        self.assertNoRestart("never restarting our own pane")
        self.assertNotIn("pilot_restart", self.transcript_kinds())
        self.assertIn("pilot_own_pane", self.transcript_kinds())

    def test_role_sharing_a_pane_is_not_respawned(self) -> None:
        """A shared pane is two agents in one window. Restarting either would kill the
        second with his input, while state never shows who sits where."""
        self.write_state(writer="agy", round=3, roles={
            "agy": {"last_seen": ago(600), "pid": self.DEAD_PID, "pane": "%7"},
            "claude": {"last_seen": ago(30), "pid": os.getpid(), "pane": "%7"},
        })
        pilot = self.pilot_with_launch()
        pilot.tick()
        self.assertNoRestart("a pane with a live neighbour sitting stays untouched")
        self.assertNotIn("pilot_restart", self.transcript_kinds())
        # The pane is "shared" no more: one of the two records belongs to a dead process,
        # and a dead record never marks the window busy. The dead role reports, with a
        # restart refusal, not an ambiguity.
        self.assertNotIn("pilot_pane_shared", self.transcript_kinds())
        self.assertIn("pilot_stale_role", self.transcript_kinds())
        impossible = [json.loads(line) for line in
                      (self.state_dir / "transcript.jsonl").read_text(encoding="utf-8").splitlines()
                      if line.strip() and json.loads(line).get("kind") == "pilot_restart_impossible"]
        self.assertIn("claude", impossible[-1]["why"])

    def test_a_squatter_recorded_in_the_other_format_is_still_a_squatter(self) -> None:
        """The same ban, but the neighbour records as a session address, not `%N`.

        A pane busy with a live neighbour never restarts: `respawn-pane` kills his
        input too. On line comparing a neighbour recorded as `mail:0.1` would read
        sitting on ANOTHER pane, the pane would count free, and the restart would
        drop a live agent. Exactly the breakage of the supervisor-own pane, only
        worse here.
        """
        self.write_state(writer="agy", round=3, roles={
            "agy": {"last_seen": ago(600), "pid": self.DEAD_PID, "pane": "%7"},
            "claude": {"last_seen": ago(30), "pid": os.getpid(), "pane": "mail:0.1"},
        })
        pilot = self.pilot_with_launch()
        # The true grouping key, not line comparing: exactly both formats reducing to one
        # key checks here.
        pilot.pane_key = lambda pane: {"%7": "mail:0.0", "mail:0.1": "mail:0.0"}.get(pane, pane)
        pilot.tick()
        self.assertNoRestart("a pane with a live neighbour sitting stays untouched")
        self.assertNotIn("pilot_restart", self.transcript_kinds())
        impossible = [json.loads(line) for line in
                      (self.state_dir / "transcript.jsonl").read_text(encoding="utf-8").splitlines()
                      if line.strip() and json.loads(line).get("kind") == "pilot_restart_impossible"]
        self.assertIn("claude", impossible[-1]["why"],
                      "a refusal must name the live neighbour, not a missing pane")

    def test_failed_respawn_is_recorded_not_hidden(self) -> None:
        self.dead_writer_state()
        pilot = self.pilot_with_launch()
        self.calls.clear()
        pairpilot.subprocess = _FakeSubprocess(self.calls, returncode=1, stderr="can't find pane")
        pilot.tick()
        self.assertIn("pilot_restart_failed", self.transcript_kinds())


class _FakeSubprocess:
    """A subprocess.run stub: writes the calls, returns a preset code."""

    def __init__(self, calls: list[list[str]], returncode: int = 0, stderr: str = "") -> None:
        self.calls = calls
        self.returncode = returncode
        self.stderr = stderr
        self.run = self._run

    def _run(self, argv, **kwargs):
        self.calls.append(list(argv))
        return type("Result", (), {"returncode": self.returncode,
                                   "stdout": "", "stderr": self.stderr})()


class TestStalledPatches(PilotCase):
    """A sealed patch nobody applied. The structural hole: `applied` never changed, so
    "applied" and "hanging since last night" read alike."""

    def sealed(self, sealed_age: int = 4000, applied: bool = False, owner: str = "codex") -> dict:
        patch = self.state_dir / "patches" / "pair-codex-1.patch"
        patch.parent.mkdir(parents=True, exist_ok=True)
        patch.write_text("--- a\n+++ b\n", encoding="utf-8")
        self.write_state(writer="agy", phase="verify", round=2,
                         roles={"agy": {"last_seen": ago(10), "pane": "%1"},
                                owner: {"last_seen": ago(10), "pane": "%2"}},
                         patches={str(patch): {"owner": owner, "sealed": True,
                                               "sealed_at": ago(sealed_age),
                                               "sha256": "deadbeef", "applied": applied}})
        return patch

    def pilot_with_grace(self, grace: float) -> "pairpilot.Pilot":
        pilot = self.pilot()
        pilot.patch_grace = grace
        return pilot

    def test_stalled_patch_wakes_the_writer_with_the_hash(self) -> None:
        self.sealed()
        pilot = self.pilot_with_grace(1800)
        pilot.tick()
        pending = pilot.pending("agy")
        self.assertIn("pair_patch_verify", pending)
        self.assertIn("deadbeef", pending)
        self.assertIn("pair_patch_applied", pending)

    def test_patch_nudge_applied_while_queued_is_dropped_not_delivered(self) -> None:
        """A rotten patch reminder in the queue never delivers (#18).

        Measured 2026-10-06: `stalled_patches` laid text in the queue before
        `patch_applied`, while `flush_outbox` delivered after — the agent took a
        false "hanging unapplied" at applied=true.
        """
        self.sealed()
        pilot = self.pilot_with_grace(1800)
        pilot.tick()
        self.assertIn("pair_patch_verify", pilot.pending("agy"))
        self.sealed(applied=True)
        pilot.tick()
        self.assertEqual("", pilot.pending("agy"),
                         "a patch applied while the text waited means nothing to deliver")
        self.assertIn("pilot_nudge_stale", self.transcript_kinds())

    def test_obligation_nudge_answered_while_queued_is_dropped_not_delivered(self) -> None:
        """A closed-request reminder never delivers.

        Measured 2026-10-07 (backlog pilot-nudge-about-closed-obligation): the
        request-370 reminder left over mailbox_fallback, though the
        obligations/000370 file already closed by an answer. The request/seq text
        checks against the obligations/<seq>-*.json file.
        """
        p = self.pilot()
        root = self.state_dir / "obligations"
        root.mkdir(parents=True, exist_ok=True)
        (root / "000370-q.json").write_text(json.dumps({
            "seq": 370, "from": "omp", "to": "agy",
            "opened_at": ago(60), "subject": "request 370",
        }), encoding="utf-8")
        state = self.write_state(roles={"agy": {"pane": "%1", "pid": os.getpid()}})
        text = "omp awaits an answer to request 370 for 30 min already"
        self.assertTrue(p.nudge_still_true(state, "agy", text),
                        "the request is open — the condition lives")
        (root / "000370-q.json").unlink()
        self.assertFalse(p.nudge_still_true(state, "agy", text),
                         "the request closed while the text waited means nothing to deliver")


    def test_patched_owner_is_told_too(self) -> None:
        """The patch author stayed silent taking it for accepted. His own job is saying
        whether it applied or not."""
        self.sealed()
        pilot = self.pilot_with_grace(1800)
        pilot.tick()
        self.assertIn("pair_patch_applied", pilot.pending("codex"))

    def test_fresh_patch_is_left_alone(self) -> None:
        self.sealed(sealed_age=60)
        self.pilot_with_grace(1800).tick()
        self.assertEqual("", self.pilot().pending("agy"), "sealed a minute ago — no defect")

    def test_applied_patch_is_not_complained_about(self) -> None:
        self.sealed(applied=True)
        self.pilot_with_grace(1800).tick()
        self.assertEqual("", self.pilot().pending("agy"))

    def test_audit_round_does_not_chase_patches(self) -> None:
        self.sealed()
        self.write_state(mode="audit", writer="agy", phase="verify", round=2,
                         roles={"agy": {"last_seen": ago(10), "pane": "%1"},
                                "codex": {"last_seen": ago(10), "pane": "%2"}},
                         patches={str(self.state_dir / "patches" / "pair-codex-1.patch"):
                                  {"owner": "codex", "sealed": True, "sealed_at": ago(4000),
                                   "applied": False}})
        self.pilot_with_grace(1800).tick()
        self.assertEqual("", self.pilot().pending("agy"), "no patch audits at all")

    def test_nag_is_sent_once(self) -> None:
        self.sealed()
        pilot = self.pilot_with_grace(1800)
        pilot.tick()
        first = pilot.pending("agy")
        pilot.tick()
        self.assertEqual(first, pilot.pending("agy"), "never sending the same letter twice")

    def test_stall_key_survives_a_path_with_slashes(self) -> None:
        """The key builds from the patch path. A slash inside the mark name drops remember()
        on the very first patch — with the overseer falling together."""
        self.sealed()
        pilot = self.pilot_with_grace(1800)
        pilot.tick()
        stamps = [p.name for p in (self.state_dir / "pilot").iterdir() if p.name.startswith(".stalled")]
        self.assertTrue(stamps, "a mark must lie flat in pilot/, never in dirs")
        self.assertTrue(all("/" not in name for name in stamps))


class TestStamps(PilotCase):
    """Marks must survive the supervisor restart, or mornings find it "forgetting"
    past neighbour pokes, writing the same anew."""

    def test_stamp_name_is_stable_across_processes(self) -> None:
        """The builtin hash() on strings randomizes every Python launch."""
        script = (
            "import importlib.util, sys;"
            f"spec=importlib.util.spec_from_loader('p', None);"
            f"m=importlib.util.module_from_spec(spec);"
            f"m.__dict__['__file__']={str(ROOT / 'bin' / 'pairpilot')!r};"
            f"exec(compile(open({str(ROOT / 'bin' / 'pairpilot')!r}, encoding='utf-8').read(),"
            f" 'p', 'exec'), m.__dict__);"
            "print(m.stamp_name('unblock-agy-Do you want to proceed?'))"
        )
        first = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                               env=dict(os.environ, PYTHONHASHSEED="1"))
        second = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                                env=dict(os.environ, PYTHONHASHSEED="999"))
        self.assertTrue(first.returncode == 0, first.stderr)
        self.assertEqual(first.stdout.strip(), second.stdout.strip(),
                         "a mark name must never depend on PYTHONHASHSEED")

    def test_stamp_name_has_no_path_separators(self) -> None:
        name = pairpilot.stamp_name("stalled-/tmp/x/.pair/patches/pair-codex-1.patch-agy")
        self.assertNotIn("/", name)


class TestCheckpoint(PilotCase):
    """In semi closing a round takes the human word. So the human must take everything
    for deciding plus the command that decides."""

    def verify_state(self, **extra) -> None:
        self.write_state(phase="verify", round=4, writer="agy",
                         roles={"agy": {"last_seen": ago(10), "pane": "%1"}}, **extra)

    def pilot_auto(self):
        pilot = self.pilot()
        pilot.autonomy = None
        return pilot

    def test_semi_without_ok_writes_a_digest(self) -> None:
        self.verify_state(autonomy="semi")
        pilot = self.pilot()
        pilot.tick()
        digest = self.state_dir / "pilot" / "checkpoint-4.md"
        self.assertTrue(digest.exists(), "the human needs a digest, not silence")
        text = digest.read_text(encoding="utf-8")
        self.assertIn("bin/pairctl ok", text)
        self.assertIn("pair_evidence", text)
        self.assertIn("pilot_checkpoint", self.transcript_kinds())

    def test_semi_with_ok_writes_no_digest(self) -> None:
        # ok counts only when said on verify/done, while a run counts only from the
        # writer — the fixture sets writer=agy, so role reads agy too. A word with no
        # phase and a fact with no role never mean the same.
        self.verify_state(autonomy="semi", human_ok=[{"round": 4, "phase": "verify"}],
                          evidence=[{"round": 4, "kind": "test", "value": "117 passed",
                                     "ok": True, "role": "agy"},
                                    {"round": 4, "kind": "ground-truth", "value": "PASS",
                                     "ok": True, "role": "agy"}])
        pilot = self.pilot()
        pilot.tick()
        self.assertFalse((self.state_dir / "pilot" / "checkpoint-4.md").exists())

    def test_semi_with_ok_but_no_evidence_still_digests(self) -> None:
        """The human word arrived, but no proof — still nothing to close with, and the
        digest must name exactly that."""
        self.verify_state(autonomy="semi", human_ok=[{"round": 4, "phase": "verify"}])
        pilot = self.pilot()
        pilot.tick()
        text = (self.state_dir / "pilot" / "checkpoint-4.md").read_text(encoding="utf-8")
        self.assertIn("pair_evidence", text)
        self.assertNotIn("pairctl ok", text.split("## What to do")[0],
                         "the human word stands already, asking again never needed")

    def test_ok_given_on_plan_is_asked_for_again(self) -> None:
        """A plan approval never opens the round close: the human must see the result and
        say "ok" already on verify."""
        self.verify_state(autonomy="semi", human_ok=[{"round": 4, "phase": "plan"}],
                          evidence=[{"round": 4, "kind": "test", "value": "117 passed",
                                     "ok": True, "role": "agy"}])
        pilot = self.pilot()
        pilot.tick()
        text = (self.state_dir / "pilot" / "checkpoint-4.md").read_text(encoding="utf-8")
        self.assertIn("bin/pairctl ok", text,
                      "a word said at the wrong checkpoint must hear anew")

    def test_auto_needs_no_human_word(self) -> None:
        self.verify_state(autonomy="auto")
        pilot = self.pilot()
        pilot.tick()
        self.assertFalse((self.state_dir / "pilot" / "checkpoint-4.md").exists())

    def test_observe_writes_no_digest(self) -> None:
        self.verify_state(autonomy="observe")
        pilot = self.pilot()
        pilot.tick()
        self.assertFalse((self.state_dir / "pilot" / "checkpoint-4.md").exists())

    def test_unapplied_patch_is_named_in_the_digest(self) -> None:
        patch = self.state_dir / "patches" / "pair-codex-9.patch"
        patch.parent.mkdir(parents=True, exist_ok=True)
        patch.write_text("x", encoding="utf-8")
        self.verify_state(autonomy="semi",
                          patches={str(patch): {"owner": "codex", "sealed": True,
                                                "sealed_at": ago(9000), "applied": False}},
                          human_ok=[{"round": 4}])
        pilot = self.pilot()
        pilot.tick()
        text = (self.state_dir / "pilot" / "checkpoint-4.md").read_text(encoding="utf-8")
        self.assertIn("pair-codex-9.patch", text)

    def test_unknown_autonomy_is_treated_as_observe(self) -> None:
        """Newer-build state must never grant this build more rights than the human
        asked."""
        self.verify_state(autonomy="turbo")
        pilot = self.pilot()
        pilot.tick()
        self.assertFalse((self.state_dir / "pilot" / "checkpoint-4.md").exists())


class TestReportNotCovered(PilotCase):
    def test_report_lists_unapplied_patches_and_failed_evidence(self) -> None:
        patch = self.state_dir / "patches" / "pair-codex-7.patch"
        patch.parent.mkdir(parents=True, exist_ok=True)
        patch.write_text("x", encoding="utf-8")
        self.write_state(phase="done", closed_at=ago(60), round=3, writer="agy", autonomy="auto",
                         roles={"agy": {"last_seen": ago(60), "pane": "%1"}},
                         evidence=[{"round": 3, "kind": "test", "value": "12 failed", "ok": False},
                                   {"round": 3, "kind": "lint", "value": "clean", "ok": True}],
                         patches={str(patch): {"owner": "codex", "sealed": True,
                                               "sealed_at": ago(9000), "applied": False}})
        pilot = self.pilot()
        pilot.tick()
        text = (self.state_dir / "REPORT.md").read_text(encoding="utf-8")
        self.assertIn("FAIL test", text)
        self.assertIn("pair-codex-7.patch", text)
        self.assertIn("NOT COVERED", text)

    def test_report_says_so_when_there_is_no_evidence(self) -> None:
        self.write_state(phase="done", closed_at=ago(60), round=3, writer="agy",
                         roles={"agy": {"last_seen": ago(60), "pane": "%1"}})
        self.pilot().tick()
        text = (self.state_dir / "REPORT.md").read_text(encoding="utf-8")
        self.assertIn("NONE. The round closed", text)

    def test_report_backlog_section_sees_active_and_archive(self) -> None:
        """Round-95 verdict: the Backlog section renders note rows from the
        active file AND the rotated archive, with real fields (never text)."""
        import json as _json
        (self.state_dir / "transcript-archive").mkdir(parents=True, exist_ok=True)
        (self.state_dir / "transcript-archive" / "1.jsonl").write_text(
            _json.dumps({"ts": "2026-10-14T21:01:45+00:00", "kind": "note",
                         "role": "omp", "note_kind": "backlog", "chars": 42}) + "\n",
            encoding="utf-8")
        (self.state_dir / "transcript.jsonl").write_text(
            _json.dumps({"ts": "2026-10-15T00:00:00+00:00", "kind": "note",
                         "role": "opencode", "note_kind": "backlog-x", "chars": 10}) + "\n",
            encoding="utf-8")
        self.write_state(phase="done", closed_at=ago(60), round=3, writer="agy",
                         roles={"agy": {"last_seen": ago(60), "pane": "%1"}})
        self.pilot().tick()
        text = (self.state_dir / "REPORT.md").read_text(encoding="utf-8")
        self.assertIn("## Backlog parked this round", text)
        self.assertIn("omp backlog (42 chars)", text)
        self.assertIn("opencode backlog-x (10 chars)", text)
        self.assertNotIn("- parked: \n", text)


class TestOperatorSafety(PilotCase):
    """The supervisor must never harm whoever launched it."""

    def test_own_pane_is_never_touched(self) -> None:
        """The pane where the supervisor itself lives is the human pane. Driving a
        reminder there means eating what he types."""
        self.inbox("opencode", 4)
        self.write_state(writer="opencode", seen={"opencode": 0},
                         roles={"opencode": {"last_seen": ago(9999), "pane": "%19"}})
        pilot = self.pilot()
        pilot.own_pane = "%19"
        pilot.tick()
        self.assertEqual("", pilot.pending("opencode"), "never laying into our own pane")
        self.assertIn("pilot_own_pane", self.transcript_kinds())

    def test_pane_of_another_role_is_still_fine(self) -> None:
        self.inbox("agy", 4)
        self.write_state(seen={"agy": 0}, roles={"agy": {"last_seen": ago(60), "pane": "%20"}})
        pilot = self.pilot()
        pilot.own_pane = "%19"
        pilot.tick()
        self.assertTrue(self.wake(pilot, "agy"), "a foreign pane is our work")

    def test_daemon_ignores_inherited_tmux_pane(self) -> None:
        """A daemon with no PAIR_PILOT_PANE never adopts an env pane.

        Measured 2026-10-07: a pilot launched from an agent pane (%3332) inherited
        TMUX_PANE, skipping the pane role forever over own_pane-skip — supervision
        silently stood. A daemon starts with a clean env, its own pane sets only
        explicit over PAIR_PILOT_PANE.
        """
        os.environ["TMUX_PANE"] = "%20"
        os.environ.pop("PAIR_PILOT_PANE", None)
        try:
            pilot = self.pilot()
            self.assertIsNone(pilot.own_pane)
        finally:
            os.environ.pop("TMUX_PANE", None)

    def test_own_pane_recorded_as_a_session_address_is_still_own(self) -> None:
        """The human pane may record as a session address, not `%N`.

        The supervisor knows its pane from TMUX_PANE, as a number, while the roster
        holds it as the `mail:0.0` address. Line comparing diverges on that, and the
        supervisor starts driving reminders into the operator pane — eating his
        input. No hypothesis: session addresses already write in `_register`, and
        the pilot reads what stands written.
        """
        self.inbox("opencode", 4)
        self.write_state(writer="opencode", seen={"opencode": 0},
                         roles={"opencode": {"last_seen": ago(9999), "pane": "mail:0.0"}})
        pilot = self.pilot()
        pilot.own_pane = "%19"
        # The true grouping key instead of a stub: comparing must check here, not that
        # tmux switches off.
        pilot.pane_key = lambda pane: {
            "mail:0.0": "mail:0.0", "%19": "mail:0.0", "%20": "mail:0.1",
        }.get(pane, pane)
        pilot.tick()
        self.assertEqual("", pilot.pending("opencode"), "never laying into our own pane")
        self.assertIn("pilot_own_pane", self.transcript_kinds())


class TestVisibleWork(PilotCase):
    """last_seen measures protocol partaking, not work."""

    def patch_composer(self, line, empty: bool) -> None:
        pairpilot.pane_alive = lambda tmux, pane: True
        pairpilot.composer_line = lambda lines: line
        pairpilot.is_composer_empty = lambda _line: empty
        # Delivery checks pane ownership (F7, round 87): in the sandbox the
        # pane is owned by construction; real belongs on a fake tmux would refuse.
        pairpilot.pane_belongs_to_project = lambda tmux, pane, root: (True, "")

    def setUp(self) -> None:
        self._line = pairpilot.composer_line
        self._empty = pairpilot.is_composer_empty
        self._belongs = pairpilot.pane_belongs_to_project
        super().setUp()

    def tearDown(self) -> None:
        pairpilot.composer_line = self._line
        pairpilot.is_composer_empty = self._empty
        pairpilot.pane_belongs_to_project = self._belongs
        super().tearDown()

    def silent_writer_state(self) -> None:
        self.write_state(writer="agy", roles={"agy": {"last_seen": ago(9999), "pane": "%9"}})

    def test_busy_pane_is_not_nudged_even_when_the_clock_ran_out(self) -> None:
        self.silent_writer_state()
        pilot = self.pilot()
        pilot.tmux = "/usr/bin/true"
        self.patch_composer("> finishing the test", empty=False)
        pilot.tick()
        self.assertEqual("", pilot.pending("agy"),
                         "an agent typing means working, never worth a push")

    def test_idle_pane_is_woken_silently(self) -> None:
        """An empty composer is a defect, once healed by typing.

        Now a file heals it. The check moved from "what stands in the print queue"
        to "what stands in the wake", because the defect reads one and the same,
        while keypress count is exactly what leaves.
        """
        self.silent_writer_state()
        pilot = self.pilot()
        pilot.tmux = "/usr/bin/true"
        self.patch_composer(">", empty=True)
        pilot.tick()
        self.assertTrue(self.wake(pilot, "agy"), "an empty composer — exactly the defect")
        self.assert_enqueued_for_push(pilot, "agy")

    def test_missing_composer_counts_as_busy(self) -> None:
        """The TUI redraws, no composer in sight. Striking now means sending keys into
        unknown what."""
        self.silent_writer_state()
        pilot = self.pilot()
        pilot.tmux = "/usr/bin/true"
        self.patch_composer(None, empty=True)
        pilot.tick()
        self.assertEqual("", pilot.pending("agy"))


class TestSmartAutoCompact(PilotCase):
    """S2 tests: Smart Auto-Compact on a hung Working... plus a done preemptive."""

    def setUp(self) -> None:
        super().setUp()
        self._orig_capture = pairpilot.capture
        self._orig_pane_alive = pairpilot.pane_alive
        self._orig_deliver = pairpilot.deliver
        self._orig_subprocess_run = pairpilot.subprocess.run
        self._orig_supports_compact = pairpilot.Pilot.supports_compact
        # By default all roles understand /compact: the existing tests check the send
        # mechanics, not the gate. The gate gets its own tests below.
        pairpilot.Pilot.supports_compact = lambda self, role: True

        self.delivered_commands = []
        self.sent_keys = []
        pairpilot.pane_alive = lambda tmux, pane: True
        pairpilot.deliver = lambda pane, text, wait_for_empty=0.0, submit=True: (
            self.delivered_commands.append((pane, text)) or {"delivered": True}
        )
        def mock_run(cmd, *args, **kwargs):
            self.sent_keys.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        pairpilot.subprocess.run = mock_run

    def tearDown(self) -> None:
        pairpilot.capture = self._orig_capture
        pairpilot.pane_alive = self._orig_pane_alive
        pairpilot.deliver = self._orig_deliver
        pairpilot.subprocess.run = self._orig_subprocess_run
        pairpilot.Pilot.supports_compact = self._orig_supports_compact
        super().tearDown()

    def test_stalled_busy_triggers_escape_and_compact(self) -> None:
        self.write_state(writer="omp", roles={"omp": {"pane": "%3", "last_seen": ago(100)}})
        pilot = self.pilot()
        pilot.tmux = "/usr/bin/true"
        pilot.stall_timeout = 10.0

        # Mocking a hung spinner: composer_line returns None (busy), the screen never
        pairpilot.composer_line = lambda lines: None
        pairpilot.capture = lambda tmux, pane: ["⎋ Working...", "Thinking..."]

        # changes. 1st tick: fixes the screen snapshot in _screen_hashes.
        pilot.tick()
        self.assertEqual([], self.delivered_commands)

        # Simulating the stall_timeout expiry with an unchanged screen.
        screen_hash, first_ts = pilot._screen_hashes["omp"]
        pilot._screen_hashes["omp"] = (screen_hash, first_ts - 15.0)

        # 2nd tick: the threshold passed, Escape and /compact send.
        pilot.tick()
        self.assertTrue(any("Escape" in cmd for cmd in self.sent_keys))
        self.assertTrue(any(text == "/compact" for pane, text in self.delivered_commands))
        self.assertIn("pilot_auto_compact", self.transcript_kinds())

    def test_active_busy_with_screen_delta_is_not_compacted(self) -> None:
        self.write_state(writer="omp", roles={"omp": {"pane": "%3", "last_seen": ago(100)}})
        pilot = self.pilot()
        pilot.tmux = "/usr/bin/true"
        pilot.stall_timeout = 10.0

        pairpilot.composer_line = lambda lines: None
        captured_lines = ["⎋ Working... token 1"]
        pairpilot.capture = lambda tmux, pane: captured_lines

        # 1st tick
        pilot.tick()
        screen_hash, first_ts = pilot._screen_hashes["omp"]

        # The screen changed (the agent truly generates output)
        captured_lines = ["⎋ Working... token 2"]
        pilot._screen_hashes["omp"] = (screen_hash, first_ts - 15.0)

        # 2nd tick: the hash changed -> the timer resets, no compact
        pilot.tick()
        self.assertEqual([], self.delivered_commands)

    def test_preventive_compact_on_round_done(self) -> None:
        self.write_state(phase="done", closed_at=now_iso(),
                         writer="agy", roles={"omp": {"pane": "%3", "last_seen": ago(10)}})
        pilot = self.pilot()
        pilot.tmux = "/usr/bin/true"

        # The composer is free
        pairpilot.composer_line = lambda lines: ">"
        pairpilot.is_composer_empty = lambda line: True
        pairpilot.capture = lambda tmux, pane: [">"]

        pilot.tick()
        self.assertTrue(any(text == "/compact" for pane, text in self.delivered_commands))

    def test_stalled_busy_without_compact_support_sends_only_escape(self) -> None:
        """A CLI with no /compact: a hung agent takes Escape, but no /compact."""
        self.write_state(writer="omp", roles={"omp": {"pane": "%3", "last_seen": ago(100)}})
        pilot = self.pilot()
        pilot.tmux = "/usr/bin/true"
        pilot.stall_timeout = 10.0
        pilot.supports_compact = lambda role: False

        pairpilot.composer_line = lambda lines: None
        pairpilot.capture = lambda tmux, pane: ["⎋ Working...", "Thinking..."]

        pilot.tick()
        screen_hash, first_ts = pilot._screen_hashes["omp"]
        pilot._screen_hashes["omp"] = (screen_hash, first_ts - 15.0)

        pilot.tick()
        self.assertTrue(any("Escape" in cmd for cmd in self.sent_keys))
        self.assertFalse(any(text == "/compact" for pane, text in self.delivered_commands))
        self.assertNotIn("pilot_auto_compact", self.transcript_kinds())

    def test_preventive_compact_skipped_without_support(self) -> None:
        """A preemptive /compact on done: an unsupported role skips."""
        self.write_state(phase="done", closed_at=now_iso(),
                         writer="agy", roles={"omp": {"pane": "%3", "last_seen": ago(10)}})
        pilot = self.pilot()
        pilot.tmux = "/usr/bin/true"
        pilot.supports_compact = lambda role: False

        pairpilot.composer_line = lambda lines: ">"
        pairpilot.is_composer_empty = lambda line: True
        pairpilot.capture = lambda tmux, pane: [">"]

        pilot.tick()
        self.assertFalse(any(text == "/compact" for pane, text in self.delivered_commands))

    def test_supports_compact_reads_profiles_hermetically(self) -> None:
        """The gate reads cli-profiles.json: True only at an explicit flag."""
        import json
        profiles = {
            "pi": {"launch": "pi", "supports_compact": True},
            "omp": {"launch": "omp", "supports_compact": True},
            "opencode": {"launch": "opencode", "supports_compact": True},
            "agy": {"launch": "agy"},
            "claude": {"launch": "claude"},
            "weird": {"launch": "x", "supports_compact": "yes"},
        }
        (self.state_dir / "cli-profiles.json").write_text(
            json.dumps(profiles), encoding="utf-8")
        pilot = self.pilot()
        saved = pairpilot.ROOT
        # setUp stubs True on the class — this test needs the real method.
        real = self._orig_supports_compact
        try:
            pairpilot.ROOT = self.state_dir
            pairpilot.Pilot.supports_compact = real
            self.assertTrue(pilot.supports_compact("pi"))
            self.assertTrue(pilot.supports_compact("omp"))
            self.assertTrue(pilot.supports_compact("opencode"))
            self.assertFalse(pilot.supports_compact("agy"))
            self.assertFalse(pilot.supports_compact("claude"))
            self.assertFalse(pilot.supports_compact("nosuchrole"))
            self.assertFalse(pilot.supports_compact("weird"))
        finally:
            pairpilot.ROOT = saved
            pairpilot.Pilot.supports_compact = lambda self, role: True
    def test_pilot_detects_and_logs_stale_server(self) -> None:
        import server_freshness
        orig = server_freshness.check_server_freshness
        try:
            server_freshness.check_server_freshness = lambda: {
                "fresh": False,
                "stale_files": ["server.py"],
                "newest_code_file": "server.py",
                "lag_seconds": 120.0,
                "why": "code changed past start",
            }
            self.write_state(writer="agy", roles={"agy": {"pane": "%1", "last_seen": ago(10)}})
            pilot = self.pilot()
            pilot.tick()
            self.assertIn("pilot_server_stale", self.transcript_kinds())
        finally:
            server_freshness.check_server_freshness = orig
    def test_stale_server_warns_even_when_parked(self) -> None:
        """The stale check knowingly lives in parking: frozen phases need fixing."""
        import server_freshness
        orig = server_freshness.check_server_freshness
        try:
            server_freshness.check_server_freshness = lambda: {
                "fresh": False,
                "stale_files": ["server.py"],
                "newest_code_file": "server.py",
                "lag_seconds": 60.0,
                "why": "code changed past start",
            }
            st = self.write_state(writer="agy", phase="done",
                                  roles={"agy": {"pane": "%1", "last_seen": ago(10)}})
            pilot = self.pilot()
            pilot.tick()
            self.assertTrue(pilot.is_done_parked(st),
                            "precondition: a done round with no tasks stays parked")
            self.assertIn("pilot_server_stale", self.transcript_kinds())
        finally:
            server_freshness.check_server_freshness = orig

class TestObligations(PilotCase):
    """An unanswered request must show. Or it dies silent in the box while the pair reads calm."""

    def obligation(self, seq: int, sender: str, receiver: str, age: int) -> None:
        root = self.state_dir / "obligations"
        root.mkdir(parents=True, exist_ok=True)
        (root / f"{seq:06d}-x.json").write_text(json.dumps({
            "seq": seq, "from": sender, "to": receiver,
            "opened_at": ago(age), "subject": f"request {seq}",
        }), encoding="utf-8")

    def test_fresh_obligation_is_left_alone(self) -> None:
        self.obligation(1, "opencode", "agy", age=60)
        self.write_state(roles={"agy": {"last_seen": ago(30), "pane": "%20"}})
        pilot = self.pilot()
        pilot.tick()
        self.assertEqual("", pilot.pending("agy"), "just asked — awaiting")

    def test_recipient_poked_within_five_minutes(self) -> None:
        """P1: a stuck one wakes inside <5 min. The 180s edge: 60s means silent, 200s means waking."""
        self.obligation(8, "opencode", "agy", age=60)
        self.write_state(roles={"agy": {"last_seen": ago(30), "pane": "%20"}})
        pilot = self.pilot()
        pilot.tick()
        self.assertEqual("", pilot.pending("agy"), "a minute means awaiting still")
        self.obligation(9, "opencode", "agy", age=200)
        pilot.tick()
        self.assertIn("mailbox holds no answer", pilot.pending("agy"))

    def test_recipient_is_poked_first(self) -> None:
        self.obligation(7, "opencode", "agy", age=1000)
        self.write_state(roles={"agy": {"last_seen": ago(30), "pane": "%20"}})
        pilot = self.pilot()
        pilot.tick()
        self.assertIn("mailbox holds no answer", pilot.pending("agy"))
        self.assertEqual("", pilot.pending("opencode"), "the author never learned yet, too early")

    def test_author_is_told_when_the_wait_stretches(self) -> None:
        self.obligation(7, "opencode", "agy", age=3000)
        self.write_state(roles={"agy": {"last_seen": ago(30), "pane": "%20"}})
        pilot = self.pilot()
        pilot.tick()
        self.assertIn("mailbox holds no answer", pilot.pending("agy"))
        self.assertIn("Do not wait forever", pilot.pending("opencode"))

    def test_answered_obligation_stops_being_nagged(self) -> None:
        self.obligation(7, "opencode", "agy", age=3000)
        (self.state_dir / "obligations" / "000007-x.json").unlink()
        self.write_state(roles={"agy": {"last_seen": ago(30), "pane": "%20"}})
        pilot = self.pilot()
        pilot.tick()
        self.assertEqual("", pilot.pending("agy"))
        self.assertEqual("", pilot.pending("opencode"))

    def test_broken_obligation_file_is_cleaned_not_crashed_on(self) -> None:
        root = self.state_dir / "obligations"
        root.mkdir(parents=True, exist_ok=True)
        (root / "000001-bad.json").write_text("{not json", encoding="utf-8")
        self.write_state(roles={"agy": {"last_seen": ago(30), "pane": "%20"}})
        pilot = self.pilot()
        pilot.tick()
        self.assertEqual([], list(root.glob("*.json")), "a broken obligation drops")

    def test_skipped_role_gets_no_obligation_nag(self) -> None:
        self.obligation(7, "codex", "agy", age=3000)
        self.write_state(roles={"agy": {"last_seen": ago(30), "pane": "%20"},
                                "codex": {"last_seen": ago(30), "pane": "%20"}})
        pilot = self.pilot()
        pilot.tick()
        self.assertEqual("", pilot.pending("agy"), "a shared pane means no poking")
    def test_dead_roles_get_no_obligation_nag(self) -> None:
        """An obligation on proven-dead roles writes no outbox escort.

        Round 99: pending_answers enqueued for dead pids with pane None —
        escorts flush could never deliver. Unknown roles (no record) still
        pass; only proven-dead records stop the push."""
        self.obligation(7, "opencode", "agy", age=3000)
        self.write_state(roles={"agy": {"last_seen": ago(3600), "pid": 998111},
                                "opencode": {"last_seen": ago(3600), "pid": 998112}})
        pilot = self.pilot()
        pilot.tick()
        self.assertEqual("", pilot.pending("agy"),
                         "no receiver escort for a dead role")
        self.assertEqual("", pilot.pending("opencode"),
                         "no author escort for a dead role")



class TestDoneParking(PilotCase):
    """Pilot-parking tests in the done phase, guarding tokens from idle burn."""

    def obligation(self, seq: int, sender: str, receiver: str, age: int) -> None:
        root = self.state_dir / "obligations"
        root.mkdir(parents=True, exist_ok=True)
        (root / f"{seq:06d}-x.json").write_text(json.dumps({
            "seq": seq, "from": sender, "to": receiver,
            "opened_at": ago(age), "subject": f"request {seq}",
        }), encoding="utf-8")

    def test_done_without_obligations_or_tasks_is_parked(self) -> None:
        # The done phase, 2 messages incoming, but no tasks and no obligations
        self.inbox("agy", 2)
        self.write_state(phase="done", round_num=65, writer="agy",
                         roles={"agy": {"last_seen": ago(1200), "pane": "%1"},
                                "omp": {"last_seen": ago(1200), "pane": "%2"}})
        pilot = self.pilot()
        self.assertTrue(pilot.is_done_parked(pilot.state()))
        pilot.tick()
        # Neither writer nor critic takes composer pushes as spam
        self.assertEqual("", pilot.pending("agy"))
        self.assertEqual("", pilot.pending("omp"))
        # Checking the pilot_parked event record in the transcript
        transcript = self.state_dir / "transcript.jsonl"
        self.assertIn("pilot_parked", transcript.read_text(encoding="utf-8"))

    def test_done_with_active_obligations_is_not_parked_and_alerts(self) -> None:
        # The done phase, but a hanging obligation stands
        self.obligation(10, "omp", "agy", age=3000)
        self.write_state(phase="done", round_num=65, writer="agy",
                         roles={"agy": {"last_seen": ago(1200), "pane": "%1"},
                                "omp": {"last_seen": ago(1200), "pane": "%2"}})
        pilot = self.pilot()
        self.assertFalse(pilot.is_done_parked(pilot.state()))
        pilot.tick()
        self.assertIn("mailbox holds no answer", pilot.pending("agy"))

    def test_active_phase_is_not_parked(self) -> None:
        # The live implement phase — the plain unread nudge works
        self.inbox("agy", 1)
        self.write_state(phase="implement", round_num=66, writer="agy",
                         roles={"agy": {"last_seen": ago(30), "pane": "%1"},
                                "omp": {"last_seen": ago(30), "pane": "%2"}})
        pilot = self.pilot()
        self.assertFalse(pilot.is_done_parked(pilot.state()))
        pilot.tick()
        self.assertIn("You have 1 unread messages", pilot.pending("agy"))


if __name__ == "__main__":
    unittest.main(verbosity=2)


