#!/usr/bin/env python3
"""Protocol tests: mailbox invariants — what a round may close over."""

from __future__ import annotations

import contextlib
import ast
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
from shutil import which as shutil_which

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pair_test_base import PAIRCHAT, SERVER, PairTestCase  # noqa: E402
from pairctl_read import project_row, render_table  # noqa: E402
from server import (  # noqa: E402
    AUTONOMIES,
    MAX_READ_WAIT_SECONDS,
    NOTE_LINE_CAP,
    NOTE_READ_MAX,
    PairSession,
    ProtocolError,
    Store,
    done_blockers,
    main,
    resolve_autonomy,
)

# The digest ceiling duplicates on purpose: the test must fall when the server
# raises its own constant above the checked one, not quietly rewrite it to match.
DIGEST_TOKEN_BUDGET_FOR_TEST = 500

def fresh_seen() -> str:
    """A fresh timestamp: the seed knowingly younger than any expiry TTL.

    Needed by the seeds of stale panes/dead processes: their intent is "drop the
    pane but keep the record", while year-old last_seen now honestly expires on
    TTL (19.3). Age runs random there; freshness pins the subject.
    """
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class TestMailboxInvariants(PairTestCase):
    def test_claim_clears_closed_at_and_phase(self) -> None:
        self.writer.tool_pair_claim(reason="I will write")
        self.write_plan(int(self.store.read().get("round", 0)))
        self.ack_plan(int(self.store.read().get("round", 0)))
        self.writer.tool_pair_advance(phase="plan")
        self.writer.tool_pair_advance(phase="implement")
        self.writer.tool_pair_advance(phase="verify")
        self.record_test_evidence()
        self.human_ok()
        self.writer.tool_pair_advance(phase="done")

        # Now round is closed
        st = self.store.read()
        self.assertEqual(st.get("phase"), "done")
        self.assertIsNotNone(st.get("closed_at"))

        # Peer claims after close -> reopens round
        res = self.peer.tool_pair_claim(reason="Starting next round")
        self.assertTrue(res.get("took_over"))
        self.assertEqual(res.get("writer"), "reviewer-agent")

        st_after = self.store.read()
        self.assertIsNone(st_after.get("closed_at"))
        self.assertEqual(st_after.get("phase"), "implement")
        self.assertEqual(st_after.get("round"), 2)

    def test_negative_unread_clamped_and_does_not_block_done(self) -> None:
        self.writer.tool_pair_claim(reason="writer")
        self.peer.tool_pair_state()
        self.writer.tool_pair_send(to="reviewer-agent", text="msg 1")
        self.peer.tool_pair_read()

        # Delete inbox files manually to simulate external file removal
        inbox = self.store.mailbox_root / "reviewer-agent"
        for p in inbox.glob("*.json"):
            p.unlink()

        # unread count must not be negative
        unread = self.writer._unread_count(self.store.read())
        self.assertEqual(unread, 0)
        self.assertEqual(self.writer.tool_pair_state()["unread"], 0)

        # Writer advances and closes round - must not be blocked by negative unread
        self.write_plan(int(self.store.read().get("round", 0)))
        self.ack_plan(int(self.store.read().get("round", 0)))
        self.writer.tool_pair_advance(phase="plan")
        self.writer.tool_pair_advance(phase="implement")
        self.writer.tool_pair_advance(phase="verify")
        self.record_test_evidence()
        self.human_ok()
        res = self.writer.tool_pair_advance(phase="done")
        self.assertEqual(res["phase"], "done")

    def test_unread_checked_under_lock_in_advance(self) -> None:
        self.writer.tool_pair_claim(reason="writer")
        self.write_plan(int(self.store.read().get("round", 0)))
        self.ack_plan(int(self.store.read().get("round", 0)))
        self.writer.tool_pair_advance(phase="plan")
        self.writer.tool_pair_advance(phase="implement")
        self.writer.tool_pair_advance(phase="verify")

        # Peer sends message to writer
        self.peer.tool_pair_send(to="writer-agent", text="review finding")

        # Writer tries to close round without reading
        self.assertRefused(
            lambda: self.writer.tool_pair_advance(phase="done"),
            contains="unread message",
        )
        self.assertEqual(self.store.read().get("phase"), "verify")

    def test_corrupt_state_does_not_clobber_message_files(self) -> None:
        self.writer.tool_pair_claim(reason="writer")
        self.peer.tool_pair_state()
        self.writer.tool_pair_send(to="reviewer-agent", text="first message")

        # Corrupt state.json
        self.store.state_path.write_text("{corrupt json", encoding="utf-8")

        # Sending message #2 must not overwrite message #1
        self.peer.tool_pair_state()
        self.writer.tool_pair_send(to="reviewer-agent", text="second message")

        files = list((self.store.mailbox_root / "reviewer-agent").glob("*.json"))
        self.assertEqual(len(files), 2)

        # Check transcript recorded state_corrupt
        transcript_events = [e.get("kind") for e in self.store.read_transcript(10)]
        self.assertIn("state_corrupt", transcript_events)

        # Reading inbox returns both messages
        read = self.peer.tool_pair_read(limit=10)
        self.assertEqual(read["count"], 2)
        self.assertEqual(read["messages"][0]["text"], "first message")
        self.assertEqual(read["messages"][1]["text"], "second message")

    def test_unreadable_message_does_not_increment_seen_silently(self) -> None:
        """Poison invariant (round 101): the wedge is named, never silent.

        The first read returns the good message as its batch; the corrupt file
        is not consumed - `seen` does not move past it, the batch stays
        pending, and `message_unreadable` is on the record. The documented
        exit is `pairctl read-ack` after escalation, never a silent skip.
        """
        self.writer.tool_pair_claim(reason="writer")
        self.peer.tool_pair_state()
        self.writer.tool_pair_send(to="reviewer-agent", text="msg 1")
        self.writer.tool_pair_send(to="reviewer-agent", text="msg 2")

        # Corrupt the second message file
        files = sorted((self.store.mailbox_root / "reviewer-agent").glob("*.json"))
        files[1].write_text("not json", encoding="utf-8")

        # Drain returns message 1 only - the corrupt one is not skipped silently
        read = self.peer.tool_pair_read(limit=5)
        self.assertEqual(read["count"], 1)
        self.assertEqual(read["messages"][0]["text"], "msg 1")

        # The batch holds; `seen` never moves past the poison until a human word.
        st = self.store.read()
        self.assertEqual(0, (st.get("seen") or {}).get("reviewer-agent", 0))
        pending = st.get("pending_reads", {}).get("reviewer-agent") or {}
        self.assertEqual(1, pending.get("cursor"),
                         "the batch covers only the good message")

        # message_unreadable must be in transcript
        transcript_events = [e.get("kind") for e in self.store.read_transcript(10)]
        self.assertIn("message_unreadable", transcript_events)

        # The poison is resolvable: skip (escalation-gated) commits the cursor.
        for _ in range(2):
            self.peer.tool_pair_read()
        import io, contextlib
        errors = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()):
            with contextlib.redirect_stderr(errors):
                from server import main as ctl_main
                rc = ctl_main(["--state", str(self.root), "read-ack",
                               "reviewer-agent", pending["batch_id"],
                               "--skip", "--reason", "corrupt file, operator reviewed"])
        self.assertEqual(0, rc, errors.getvalue())
        st = self.store.read()
        self.assertEqual(1, st["seen"]["reviewer-agent"],
                         "the skip commits exactly the batch cursor, not past it")

    def test_pair_deliver_rejects_self_and_empty(self) -> None:
        self.assertRefused(
            lambda: self.writer.tool_pair_deliver(to="writer-agent", text="hi"),
            contains="cannot deliver a message to your own role",
        )
        self.assertRefused(
            lambda: self.writer.tool_pair_deliver(to="reviewer-agent", text="   "),
            contains="text` is required",
        )

    def test_legal_next_phases_tailored_to_role(self) -> None:
        self.writer.tool_pair_claim(reason="writer")
        self.writer.tool_pair_advance("plan")

        writer_state = self.writer.tool_pair_state()
        self.assertIn("implement", writer_state["legal_next_phases"])
        self.assertIn("scoping", writer_state["legal_next_phases"])

        peer_state = self.peer.tool_pair_state()
        self.assertEqual(peer_state["legal_next_phases"], ["scoping"])
        self.assertNotIn("implement", peer_state["legal_next_phases"])

    def test_allowed_transitions_not_duplicated(self) -> None:
        self.writer.tool_pair_claim(reason="writer")
        self.write_plan(int(self.store.read().get("round", 0)))
        self.ack_plan(int(self.store.read().get("round", 0)))
        self.writer.tool_pair_advance("plan")
        self.writer.tool_pair_advance("implement")
        self.writer.tool_pair_advance("verify")
        self.record_test_evidence()
        self.human_ok()
        self.writer.tool_pair_advance("done")

        self.assertRefused(
            lambda: self.writer.tool_pair_advance("scoping"),
            contains="allowed: implement.",
        )

    def test_duo_roles_registration_and_stale_pane_cleanup(self) -> None:
        # Prepopulate state with a stale role that has a dead PID holding pane %99
        def seed(state):
            roles = state.setdefault("roles", {})
            roles["old-agent"] = {
                "first_seen": fresh_seen(),
                "last_seen": fresh_seen(),
                "pid": 99999999,
                "pane": "%99",
            }
        self.store.mutate(seed)

        # Now start an agent in pane %99
        orig_pane = os.environ.get("TMUX_PANE")
        try:
            # `%99` and `%100` are phantoms: tmux answers them with code 0 and an empty
            # output. The suite used to demand such addresses land in the roster as ours,
            # passing exactly on missing panes. Now `_register` refuses writing a pane
            # that cannot prove itself, so a refusal with a transcript record checks here,
            # while the stale record still drops as before.
            # the stale record still drops as before.
            os.environ["TMUX_PANE"] = "%99"
            duo_writer = PairSession("opencode-writer", self.store)
            duo_critic = PairSession("opencode-critic", self.store)

            # Trigger registration
            duo_writer.tool_pair_state()

            state = self.store.read()
            # Stale role's pane should have been cleared
            self.assertNotIn("pane", state["roles"]["old-agent"])
            # A phantom pane never proves itself ours, never lands in the roster
            self.assertNotIn("pane", state["roles"]["opencode-writer"])
            kinds = [e["kind"] for e in self.store.read_transcript()]
            self.assertIn("pane_env_stale", kinds)

            # Critic can also register without conflict
            os.environ["TMUX_PANE"] = "%100"
            duo_critic.tool_pair_state()
            state = self.store.read()
            self.assertNotIn("pane", state["roles"]["opencode-critic"])
        finally:
            if orig_pane is not None:
                os.environ["TMUX_PANE"] = orig_pane
            else:
                os.environ.pop("TMUX_PANE", None)

    def test_peer_status_reports_dead_process(self) -> None:
        """An end-to-end check through `pair_state`: a dead neighbour shows in the answer.

        The answer logic itself lives in `liveness` and checks in `test_liveness.py` —
        one test file per source. What matters here is different: the `pair_state`
        answer truly carries the field, and an agent reading state learns the
        neighbour silence.
        """
        def seed(state):
            roles = state.setdefault("roles", {})
            roles["ghost-agent"] = {
                "first_seen": fresh_seen(),
                "last_seen": fresh_seen(),
                "pid": 99999999,
            }
        self.store.mutate(seed)

        status = self.writer.tool_pair_state()
        ghost = status["peers"]["ghost-agent"]
        self.assertFalse(ghost["alive"])
        self.assertFalse(ghost["will_read_new_messages"])
        self.assertIn("dead", ghost["hint"].lower())

    def test_unverified_delivery_still_reaches_the_mailbox(self) -> None:
        """An unconfirmed POINTER must never turn into silence.

        A text wall used to stand in the composer here, and on a broken match it had
        to duplicate into the box — hence the double trace at the human. Now one
        pointer line goes to the composer, the text always lies in the box before
        printing. So nothing checks here: the letter in the box IS the delivery, and
        the pointer outcome never affects it.
        """
        def seed(state):
            state.setdefault("roles", {})["reviewer-agent"] = {
                "first_seen": fresh_seen(),
                "last_seen": fresh_seen(),
                "pid": os.getpid(),
                "pane": "%55",
            }
        self.store.mutate(seed)

        import composer
        orig_deliver = composer.deliver
        orig_foreign = composer.pane_is_foreign
        self.allow_panes()

        def mock_deliver(pane, text, wait_for_empty=0.0, submit=True):
            return {"delivered": False, "unverified": True, "chars": len(text),
                    "why": "the insert never confirmed on screen"}

        composer.deliver = mock_deliver
        try:
            res = self.writer.tool_pair_deliver(to="reviewer-agent", text="payload worth keeping")
            self.assertTrue(res["delivered"], "the box is the main path, never bound to a pane")
            self.assertFalse(res["in_pane"], "the pointer never confirmed")
            self.assertIn("mailbox", res["why"])
            read = self.peer.tool_pair_read(wait_seconds=0)
            self.assertEqual(read["count"], 1)
            self.assertIn("payload worth keeping", read["messages"][0]["text"])
        finally:
            composer.deliver = orig_deliver
            composer.pane_is_foreign = orig_foreign

    def test_deliver_composer_busy_fallback_to_mailbox(self) -> None:
        # Prepopulate reviewer pane
        def seed(state):
            roles = state.setdefault("roles", {})
            roles["reviewer-agent"] = {
                "first_seen": fresh_seen(),
                "last_seen": fresh_seen(),
                "pid": os.getpid(),
                "pane": "%55",
            }
        self.store.mutate(seed)

        import composer
        orig_deliver = composer.deliver
        orig_foreign = composer.pane_is_foreign
        self.allow_panes()

        def mock_deliver(pane, text, wait_for_empty=0.0, submit=True):
            raise composer.ComposerBusy("user is actively typing")

        composer.deliver = mock_deliver
        try:
            res = self.writer.tool_pair_deliver(to="reviewer-agent", text="important payload")
            # Delivery SUCCEEDS though nothing left for the composer: by the human directive
            # the box IS the carrier, while a busy composer means "pointer undelivered",
            # never "message lost". The prior check demanded `delivered: False` with
            # `fallback_sent`, checking the old order where the box stood a backup path,
            # not the main one.
            self.assertTrue(res["delivered"])
            self.assertFalse(res["in_pane"], "a busy composer means no typing")
            self.assertIn("busy", res["why"])

            # Verify message landed in reviewer's mailbox
            read = self.peer.tool_pair_read(wait_seconds=0)
            self.assertEqual(read["count"], 1)
            self.assertIn("important payload", read["messages"][0]["text"])
        finally:
            composer.deliver = orig_deliver
            composer.pane_is_foreign = orig_foreign
