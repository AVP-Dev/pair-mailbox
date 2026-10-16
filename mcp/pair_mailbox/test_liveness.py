"""Neighbour-liveness tests: will he read a new message.

One test file per source: `liveness.py` owns one thing — what every party sees
about the rest — checking here.

The `alive_check` check stubs everywhere process death speaks: the test must never
depend on whether the PID lives at run time. One real-call test stays on purpose —
or the stub would hide a typo in the check itself.
"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import liveness  # noqa: E402


def state_with(**kwargs) -> dict:
    base = {"phase": "implement", "roles": {}}
    base.update(kwargs)
    return base


def dead(_pid) -> bool:
    return False


def alive(_pid) -> bool:
    return True


class TestPidAlive(unittest.TestCase):
    def test_own_pid_is_alive(self) -> None:
        "A single test on the real call: a stub would hide a typo."
        self.assertTrue(liveness.pid_alive(os.getpid()))

    def test_missing_pid_is_not_alive(self) -> None:
        self.assertFalse(liveness.pid_alive(None))

    def test_zero_is_not_alive(self) -> None:
        self.assertFalse(liveness.pid_alive(0))

    def test_negative_pid_is_not_alive(self) -> None:
        self.assertFalse(liveness.pid_alive(-1))

    def test_dead_pid_is_not_alive(self) -> None:
        self.assertFalse(liveness.pid_alive(99999999))

    def test_garbage_pid_does_not_raise(self) -> None:
        """A roster role may arrive with anything instead of a pid.

        An exception here would drop `pair_state` whole — the agent learning nothing
        about nobody over a foreign broken record.
        """
        for bad in ("not a number", "", object(), 3.5):
            self.assertFalse(liveness.pid_alive(bad), bad)

    def test_bool_is_an_int_in_python_and_survives_as_alive(self) -> None:
        """`True` passes the `isinstance(pid, int)` check — as before.

        Pinned, not fixed: this is `server.is_pid_alive` behaviour carried verbatim,
        and changing it here means changing the protocol past the task. A roster value
        can never be `True`: a pid or a number stands there, or nothing. The test
        names it straight so a fix never reads accidental one day.
        """
        self.assertTrue(liveness.pid_alive(True))

    def test_eperm_means_alive_not_dead(self) -> None:
        """A foreign process means living, merely not ours.

        The neighbour may launch as another user. Declaring him dead means never
        awaiting an answer that arrives, writing the next round into the void.
        """
        import errno as _errno
        import unittest.mock as _mock

        with _mock.patch.object(liveness.os, "kill",
                               side_effect=OSError(_errno.EPERM, "not yours")):
            self.assertTrue(liveness.pid_alive(os.getpid()))

    def test_other_oserror_means_dead(self) -> None:
        import errno as _errno
        import unittest.mock as _mock

        with _mock.patch.object(liveness.os, "kill",
                               side_effect=OSError(_errno.ESRCH, "no such process")):
            self.assertFalse(liveness.pid_alive(os.getpid()))

class TestPidSlotAlive(unittest.TestCase):
    """Round 103 S3: pid + /proc starttime (f22) — a reused pid is DEAD.

    The roster staleness rule: `os.kill(pid, 0)` proves a process holds the
    pid, not OUR process. Live proof (round 102): the opencode server killed
    at 17:30Z stayed "alive" for three hours.
    """

    def test_real_own_process_true(self) -> None:
        import subprocess
        proc = subprocess.Popen(["sleep", "30"])
        try:
            start = liveness.proc_starttime(proc.pid)
            self.assertIsNotNone(start)
            self.assertTrue(liveness.pid_slot_alive(proc.pid, start))
        finally:
            proc.kill()

    def test_reused_pid_reads_dead(self) -> None:
        """THE red proof: pid reused by an unrelated live process must read
        dead, though os.kill says alive."""
        import subprocess
        proc = subprocess.Popen(["sleep", "30"])
        try:
            start = liveness.proc_starttime(proc.pid)
            self.assertIsNotNone(start)
            self.assertFalse(liveness.pid_slot_alive(proc.pid, start - 1),
                             "a mismatched starttime is a reused pid: DEAD")
            self.assertTrue(liveness.pid_alive(proc.pid),
                            "sanity: os.kill still says alive -> slot check adds the fact")
        finally:
            proc.kill()

    def test_no_proc_records_fallback_alive(self) -> None:
        """Stated fallback: an entry with no recorded starttime (pre-S3 roster)
        keeps the coarser os.kill answer; a pid that has no /proc either reads
        coarsely, never guessed."""
        real = liveness.pid_alive(os.getpid())
        self.assertTrue(liveness.pid_slot_alive(os.getpid(), None) == real)
        self.assertFalse(liveness.pid_slot_alive(99999999, None))

    def test_peer_status_honours_pid_start(self) -> None:
        """With pid_start recorded, a reused pid reads DEAD in the status."""
        status = liveness.peer_status(
            state_with(roles={"pi": {"pid": 4242, "pid_start": 111111}}),
            role="opencode", writer="opencode",
            alive_check=alive,  # os.kill stub says alive: the slot decides
        )
        self.assertFalse(status["pi"]["alive"])

    def test_peer_status_without_pid_start_unchanged(self) -> None:
        """Pre-S3 entry: the old coarser check stands (stated fallback)."""
        status = liveness.peer_status(
            state_with(roles={"pi": {"pid": 4242}}),
            role="opencode", writer="opencode", alive_check=alive,
        )
        self.assertTrue(status["pi"]["alive"])


class TestPeerStatusShape(unittest.TestCase):
    def test_self_is_absent(self) -> None:
        "Seeing our own role in the neighbour list is nothing: no neighbour."
        status = liveness.peer_status(
            state_with(roles={"opencode": {"pid": 1}, "pi": {"pid": 2}}),
            role="opencode", writer="opencode", alive_check=alive,
        )
        self.assertEqual(list(status), ["pi"])

    def test_roles_are_labelled_against_the_writer(self) -> None:
        "Who writes sets the layout, not the roster registration fact."
        status = liveness.peer_status(
            state_with(roles={"pi": {"pid": 2}, "agy": {"pid": 3}}),
            role="opencode", writer="pi", alive_check=alive,
        )
        self.assertEqual(status["pi"]["role"], "writer")
        self.assertEqual(status["agy"]["role"], "critic")

    def test_no_peers_gives_an_empty_mapping(self) -> None:
        status = liveness.peer_status(state_with(), role="opencode", writer=None)
        self.assertEqual(status, {})


class TestWillReadNewMessages(unittest.TestCase):
    def test_live_peer_in_an_open_round_reads(self) -> None:
        status = liveness.peer_status(
            state_with(roles={"pi": {"pid": 2}}), role="opencode",
            writer="opencode", alive_check=alive,
        )
        peer = status["pi"]
        self.assertTrue(peer["alive"])
        self.assertTrue(peer["will_read_new_messages"])
        self.assertEqual(peer["hint"], "Listening.")

    def test_dead_peer_does_not_read_and_is_named(self) -> None:
        """Neighbour silence must speak aloud, with his pid.

        Or the letter leaves for a closed round, and the finding never arrives.
        """
        status = liveness.peer_status(
            state_with(roles={"pi": {"pid": 4242}}), role="opencode",
            writer="opencode", alive_check=dead,
        )
        peer = status["pi"]
        self.assertFalse(peer["alive"])
        self.assertFalse(peer["will_read_new_messages"])
        self.assertIn("4242", peer["hint"])

    def test_closed_round_beats_a_live_process(self) -> None:
        """The process lives, but the round closed — nothing new reads.

        The reverse check order would lie the other way: a live pid would read as a
        ready neighbour, and the letter would leave nowhere with not a word.
        """
        status = liveness.peer_status(
            state_with(phase="done", roles={"pi": {"pid": 2}}),
            role="opencode", writer="opencode", alive_check=alive,
        )
        peer = status["pi"]
        self.assertTrue(peer["round_closed"])
        self.assertFalse(peer["will_read_new_messages"])
        self.assertIn("closed", peer["hint"].lower())

    def test_hint_names_the_way_back(self) -> None:
        "A hint must name the deed, not only state the fact."
        status = liveness.peer_status(
            state_with(phase="done", roles={"pi": {"pid": 2}}),
            role="opencode", writer="opencode", alive_check=alive,
        )
        self.assertIn("pair_advance", status["pi"]["hint"])


class TestMuteAgent(unittest.TestCase):
    "The server lives, no agent: the roster pid is the server one (R85, pi)."

    def test_never_spoke_does_not_read(self) -> None:
        status = liveness.peer_status(
            state_with(roles={"pi": {
                "first_seen": "2020-01-01T00:00:00+00:00",
                "last_seen": "2020-01-05T00:00:00+00:00",
                "pid": 99, "sends": 0, "reads": 0}}),
            role="opencode", writer="opencode", alive_check=alive,
        )
        peer = status["pi"]
        self.assertTrue(peer["alive"])
        self.assertFalse(peer["will_read_new_messages"])
        self.assertIn("never spoke", peer["hint"])

    def test_sender_reads_despite_old_registration(self) -> None:
        status = liveness.peer_status(
            state_with(roles={"pi": {
                "first_seen": "2020-01-01T00:00:00+00:00",
                "last_seen": "2020-01-05T00:00:00+00:00",
                "pid": 99, "sends": 3}}),
            role="opencode", writer="opencode", alive_check=alive,
        )
        self.assertTrue(status["pi"]["will_read_new_messages"])

    def test_fresh_registration_gets_grace(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        status = liveness.peer_status(
            state_with(roles={"pi": {
                "first_seen": now, "last_seen": now,
                "pid": 99, "sends": 0}}),
            role="opencode", writer="opencode", alive_check=alive,
        )
        self.assertTrue(status["pi"]["will_read_new_messages"])


class TestSilentSeconds(unittest.TestCase):
    def test_counted_from_last_seen(self) -> None:
        status = liveness.peer_status(
            state_with(roles={"pi": {"pid": 2, "last_seen": "2026-01-01T00:00:00+00:00"}}),
            role="opencode", writer="opencode", alive_check=alive,
        )
        self.assertGreater(status["pi"]["silent_seconds"], 0)

    def test_missing_last_seen_is_none_not_a_crash(self) -> None:
        status = liveness.peer_status(
            state_with(roles={"pi": {"pid": 2}}), role="opencode",
            writer="opencode", alive_check=alive,
        )
        self.assertIsNone(status["pi"]["silent_seconds"])

    def test_unparseable_last_seen_is_none_not_a_crash(self) -> None:
        "A broken timestamp never counts age, never drops the answer."
        status = liveness.peer_status(
            state_with(roles={"pi": {"pid": 2, "last_seen": "yesterday"}}),
            role="opencode", writer="opencode", alive_check=alive,
        )
        self.assertIsNone(status["pi"]["silent_seconds"])
        self.assertIn("pi", status)


class TestUnreadCounters(unittest.TestCase):
    "P1: a stuck one shows in seconds. The per-role unread count lives in peers."

    def test_unread_arrives_with_the_count(self) -> None:
        status = liveness.peer_status(
            state_with(roles={"pi": {"pid": 2}}), role="opencode",
            writer="opencode", alive_check=alive, unread={"pi": 3},
        )
        self.assertEqual(3, status["pi"]["unread"])

    def test_no_count_means_no_field_not_zero(self) -> None:
        "Zero would mean \"all read\", while missing data means unknown."
        status = liveness.peer_status(
            state_with(roles={"pi": {"pid": 2}}), role="opencode",
            writer="opencode", alive_check=alive,
        )
        self.assertNotIn("unread", status["pi"])



if __name__ == "__main__":
    unittest.main(verbosity=2)