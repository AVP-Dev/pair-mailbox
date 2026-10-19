from __future__ import annotations

import json
import time
import unittest

import read_delivery
from pair_test_base import PairTestCase
from server import ProtocolError

class ReadDeliveryState(unittest.TestCase):
    def test_replay_escalates_once_without_advancing_cursor(self) -> None:
        state = {"seen": {"opencode": 0}}
        batch_id = read_delivery.create_pending_batch(
            state, "opencode", 0, 2, [41, 44])
        pending, attempts, escalated = read_delivery.replay_attempt(state, "opencode")
        self.assertEqual(batch_id, pending["batch_id"])
        self.assertEqual(2, attempts)
        self.assertFalse(escalated)
        pending, attempts, escalated = read_delivery.replay_attempt(state, "opencode")
        self.assertEqual(3, attempts)
        self.assertTrue(escalated)
        self.assertTrue(pending["escalated"])
        self.assertEqual([41, 44], pending["seqs"])
        self.assertEqual(0, state["seen"]["opencode"], "replay never consumes mail")
        _pending, attempts, escalated = read_delivery.replay_attempt(state, "opencode")
        self.assertEqual(3, attempts, "the visible retry count saturates")
        self.assertFalse(escalated, "escalation is recorded once")

    def test_ack_commits_only_the_pending_cursor_once(self) -> None:
        state = {"seen": {"opencode": 4}}
        batch_id = read_delivery.create_pending_batch(
            state, "opencode", 4, 6, [91, 93])
        receipt = read_delivery.acknowledge_batch(state, "opencode", batch_id)
        self.assertEqual({
            "decision": "ack", "batch_id": batch_id, "seqs": [91, 93], "reason": "",
        }, receipt)
        self.assertEqual(6, state["seen"]["opencode"])
        self.assertNotIn("opencode", state["pending_reads"])
        with self.assertRaisesRegex(ValueError, "unknown or already acknowledged"):
            read_delivery.acknowledge_batch(state, "opencode", batch_id)

    def test_operator_skip_requires_reason_and_escalation(self) -> None:
        state = {"seen": {"opencode": 0}}
        batch_id = read_delivery.create_pending_batch(state, "opencode", 0, 1, [7])
        with self.assertRaisesRegex(ValueError, "only after replay escalation"):
            read_delivery.acknowledge_batch(
                state, "opencode", batch_id, decision="skip", reason="obsolete",
                require_escalated=True,
            )
        for _ in range(2):
            read_delivery.replay_attempt(state, "opencode")
        with self.assertRaisesRegex(ValueError, "skip requires"):
            read_delivery.acknowledge_batch(
                state, "opencode", batch_id, decision="skip", require_escalated=True,
            )
        receipt = read_delivery.acknowledge_batch(
            state, "opencode", batch_id, decision="skip", reason="reviewed, obsolete",
            require_escalated=True,
        )
        self.assertEqual("skip", receipt["decision"])
        self.assertEqual("reviewed, obsolete", receipt["reason"])
        self.assertEqual(1, state["seen"]["opencode"])

    def test_wrong_token_never_advances(self) -> None:
        state = {"seen": {"omp": 2}}
        read_delivery.create_pending_batch(state, "omp", 2, 3, [15])
        with self.assertRaisesRegex(ValueError, "unknown or already acknowledged"):
            read_delivery.acknowledge_batch(state, "omp", "not-the-token")
        self.assertEqual(2, state["seen"]["omp"])

    def test_attempts_live_inside_the_batch_not_a_second_key(self) -> None:
        state = {"seen": {"opencode": 0}}
        read_delivery.create_pending_batch(state, "opencode", 0, 1, [7])
        read_delivery.replay_attempt(state, "opencode")
        self.assertNotIn("_read_attempts", state,
                         "attempts must not leak into the shared state document")
        self.assertEqual(2, state["pending_reads"]["opencode"]["attempts"])

    def test_legacy_attempt_counter_migrates_into_the_batch(self) -> None:
        state = {"seen": {"opencode": 0}, "_read_attempts": {"opencode": 3}}
        read_delivery.create_pending_batch(state, "opencode", 0, 1, [7])
        read_delivery._migrate_legacy(state, "opencode")
        self.assertNotIn("_read_attempts", state)
        self.assertEqual(3, state["pending_reads"]["opencode"]["attempts"])

    def test_drain_refusal_shares_the_ack_drift_cause(self) -> None:
        state = {"seen": {"opencode": 0}}
        read_delivery.create_pending_batch(state, "opencode", 0, 1, [7])
        state["seen"]["opencode"] = 5  # cursor moved under the batch
        with self.assertRaisesRegex(ValueError,
                                    "read cursor changed since the pending batch"):
            read_delivery.acknowledge_batch(state, "opencode",
                                            state["pending_reads"]["opencode"]["batch_id"])
        self.assertEqual(5, state["seen"]["opencode"])

    def test_drain_time_drift_refuses_too(self) -> None:
        state = {"seen": {"opencode": 0}}
        read_delivery.create_pending_batch(state, "opencode", 0, 1, [7])

        class Sess:
            role = "opencode"
            store = type("S", (), {"mutate": staticmethod(lambda f: f(state)),
                                   "append_transcript": staticmethod(lambda *a, **k: None),
                                   "root": None})()
            def _register(self, state): pass
            def _inbox_files(self): return []
        state["seen"]["opencode"] = 5  # cursor moved under the batch
        with self.assertRaisesRegex(ValueError,
                                    "read cursor changed since the pending batch"):
            read_delivery.drain(Sess(), 10)

    def test_withheld_count_names_the_letters_behind_the_batch(self) -> None:
        """The wedge must have a number: letters no read will bring.

        A batch that is not acknowledged holds the cursor, so a letter that
        arrives after it stays invisible. The count is what tells the reader
        that reading again cannot help and acknowledging can.
        """
        state = {"seen": {"opencode": 0}}
        read_delivery.create_pending_batch(state, "opencode", 0, 2, [41, 44])
        self.assertEqual(0, read_delivery.withheld_count(state, "opencode", 2),
                         "a fresh batch withholds nothing")
        self.assertEqual(3, read_delivery.withheld_count(state, "opencode", 5),
                         "letters that arrived after the batch are counted")
        state["pending_reads"]["opencode"]["fresh_below"] = {"start": 2, "cursor": 4}
        self.assertEqual(1, read_delivery.withheld_count(state, "opencode", 5),
                         "the fresh batch below a hole shows its own letters")
        self.assertEqual(0, read_delivery.withheld_count({"seen": {}}, "opencode", 5),
                         "no batch, no wedge, nothing withheld")



class ReadDeliveryWaiting(PairTestCase):
    """What a read says about its wait, and about the wedge it sits in.

    Measured 2026-10-19: a read whose batch was still unacknowledged answered
    in 1.24s with the same letters, whatever wait the agent asked for, and
    reported "waited 45 seconds" for it. The agent read again at once, again
    and again - 29k polls a day, every poll a full context. The spin was the
    symptom; the disease was an answer that lied about the wait and hid the
    wedge behind it.
    """

    def test_a_replayed_batch_spends_the_wait_it_was_given(self) -> None:
        self.writer.tool_pair_send(to="reviewer-agent", text="one letter")
        first = self.peer.tool_pair_read(wait_seconds=0)
        self.assertEqual(1, first["count"])
        started = time.monotonic()
        again = self.peer.tool_pair_read(wait_seconds=2)
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 1.9,
                                "a replay must not answer in milliseconds")
        self.assertGreaterEqual(again["waited_seconds"], 1.9,
                                "the answer must report the time the call really took")
        self.assertEqual(2.0, again["wait_budget_seconds"])
        self.assertTrue(again["must_ack"], "the batch is still unacknowledged")

    def test_a_letter_behind_an_unacked_batch_is_named_as_withheld(self) -> None:
        self.writer.tool_pair_send(to="reviewer-agent", text="first")
        first = self.peer.tool_pair_read(wait_seconds=0)
        self.assertEqual(0, first["letters_behind"], "nothing waits behind a fresh batch")
        self.writer.tool_pair_send(to="reviewer-agent", text="second")
        wedged = self.peer.tool_pair_read(wait_seconds=0)
        self.assertEqual(["first"], [m["text"] for m in wedged["messages"]],
                         "the same batch, not the new letter")
        self.assertEqual(1, wedged["letters_behind"],
                         "the letter waiting behind the batch must be counted")
        self.assertTrue(wedged["must_ack"], "the wedge is still unacknowledged")
        released = self.peer.tool_pair_read(wait_seconds=0, ack_token=first["ack_token"])
        self.assertEqual(["second"], [m["text"] for m in released["messages"]],
                         "the ack releases the letter that was waiting")
        self.assertEqual(0, released["letters_behind"], "nothing waits behind it now")


class ReadDeliverySurfaces(PairTestCase):
    """The pending batch must be visible outside pair_read itself.

    Round-101 scenario: a batch was returned, the client tore the connection,
    and nothing in state or digest said the mail was read-but-unacked — the
    agent's next orientation read "all clear" while its reply was never sent.
    """

    def test_pair_state_carries_the_pending_batch(self) -> None:
        self.writer.tool_pair_send(to="reviewer-agent", text="answer me")
        self.peer.tool_pair_read()
        st = self.peer.tool_pair_state()
        pending = st.get("pending_read")
        self.assertIsNotNone(pending, "state must show a returned-but-unacked batch")
        self.assertEqual(1, pending["attempts"])
        self.assertTrue(pending["seqs"])
        self.assertFalse(pending["escalated"])

    def test_digest_names_the_pending_batch(self) -> None:
        self.writer.tool_pair_send(to="reviewer-agent", text="seen by digest")
        self.peer.tool_pair_read()
        text = self.peer.tool_pair_digest()
        self.assertIn("PENDING READ", str(text))

    def test_ack_clears_state_and_digest(self) -> None:
        self.writer.tool_pair_send(to="reviewer-agent", text="ack path")
        batch = self.peer.tool_pair_read()
        self.peer.tool_pair_read(ack_token=batch["ack_token"])
        st = self.peer.tool_pair_state()
        self.assertIsNone(st.get("pending_read"))
        text = self.peer.tool_pair_digest()
        self.assertNotIn("PENDING READ", str(text))


class ReadDeliveryReplay(PairTestCase):
    """The live failure the slice exists for: read -> discard reply -> retry.

    Seq 1440 was consumed by a drain whose reply never reached the agent. The
    retry must return the SAME letters and the SAME token; a replay after the
    file set shifted must refuse by name, never serve shifted letters.
    """

    def _batch_of(self, text: str) -> dict:
        self.writer.tool_pair_send(to="reviewer-agent", text=text)
        return self.peer.tool_pair_read()

    def test_retry_returns_the_same_letters_and_token(self) -> None:
        first = self._batch_of("the one letter")
        second = self.peer.tool_pair_read()
        self.assertEqual(first["ack_token"], second["ack_token"],
                         "replay must keep the batch token")
        self.assertEqual([m["text"] for m in first["messages"]],
                         [m["text"] for m in second["messages"]],
                         "replay must serve the same letters, not the next ones")
        self.assertTrue(second["pending_read"]["replayed"])

    def test_new_mail_after_batch_waits_for_the_ack(self) -> None:
        first = self._batch_of("first letter")
        self.writer.tool_pair_send(to="reviewer-agent", text="second letter")
        replay = self.peer.tool_pair_read()
        self.assertEqual([m["text"] for m in replay["messages"]],
                         ["first letter"],
                         "a replay never mixes in later mail")
        self.peer.tool_pair_read(ack_token=first["ack_token"])
        after = self.peer.tool_pair_read()
        self.assertEqual([m["text"] for m in after["messages"]], ["second letter"])

    def test_replayed_batch_survives_a_restart(self) -> None:
        import server as srv

        first = self._batch_of("restart proof")
        revived = srv.PairSession(self.peer.role, self.store)
        replay = revived.tool_pair_read()
        self.assertEqual(first["ack_token"], replay["ack_token"],
                         "the pending batch must live in state, not in memory")
        revived.tool_pair_read(ack_token=first["ack_token"])

    def test_missing_recorded_file_names_the_poison_and_keeps_serving(self) -> None:
        """Poison invariant: the wedge is named, the channel keeps moving.

        The vanish/unreadable/inserted cases used to abort the whole drain.
        Per the agreed invariant (seq 1468-1472): the poisoned batch stays
        pending with its token, pair_state names the path and the cure, and
        letters after the hole keep draining as fresh batches.
        """
        self._batch_of("vanishing letter")
        for path in sorted((self.store.mailbox_root / self.peer.role).glob("*.json")):
            path.unlink()
        answer = self.peer.tool_pair_read()
        st = self.peer.tool_pair_state()
        pending = st["pending_read"]
        self.assertIn("vanished", pending["poison"]["cause"])
        self.assertIn("pairctl read-ack", pending["poison"]["cure"])

    def test_lower_seq_inserted_inside_the_range_names_the_poison(self) -> None:
        batch = self._batch_of("range letter")["pending_read"]
        recorded_seqs = batch["seqs"]
        inbox = self.store.mailbox_root / self.peer.role
        recorded = sorted(inbox.glob("*.json"))
        low = json.loads(recorded[0].read_text(encoding="utf-8"))
        low["seq"] = 1
        if 1 in recorded_seqs:
            low["seq"] = recorded_seqs[0] - 1
        (inbox / "00000001-inserted.json").write_text(
            json.dumps(low), encoding="utf-8")
        self.peer.tool_pair_read()
        pending = self.peer.tool_pair_state()["pending_read"]
        self.assertIn("inside the pending batch range", pending["poison"]["cause"])

    def test_poisoned_batch_does_not_stop_the_channel(self) -> None:
        """(i)-(iv) from the agreed pin: seen stays at the hole, fresh acks
        record runs, the hole's resolution folds them in, and acked runs never
        re-serve."""
        # Batch over the first letter, then poison its file.
        first = self._batch_of("poisoned letter")["pending_read"]
        inbox = self.store.mailbox_root / self.peer.role
        poison_path = sorted(inbox.glob("*.json"))[0]
        poison_path.write_text("not json", encoding="utf-8")

        # Fresh letters arrive after the hole and drain as their own batch.
        self.writer.tool_pair_send(to="reviewer-agent", text="after 1")
        self.writer.tool_pair_send(to="reviewer-agent", text="after 2")
        fresh = self.peer.tool_pair_read()
        self.assertEqual(["after 1", "after 2"],
                         [m["text"] for m in fresh["messages"]])
        self.assertEqual(0, (self.store.read().get("seen") or {}).get(self.peer.role, 0),
                         "(i) seen stays at the hole while it stands")
        st = self.store.read()
        pending = st["pending_reads"][self.peer.role]
        self.assertTrue(pending.get("fresh_below"),
                         "(i) the fresh batch rides below the hole, unacked")

        # A later drain must not re-serve the acked fresh range.
        self.peer.tool_pair_read(ack_token=fresh["ack_token"])
        self.writer.tool_pair_send(to="reviewer-agent", text="after 3")
        later = self.peer.tool_pair_read()
        self.assertEqual(["after 3"], [m["text"] for m in later["messages"]],
                         "(iv) acked runs are consulted, not re-served")

        # Resolving the wedge folds the recorded runs into `seen`. The one
        # letter after the last fold ("after 3", unacked) becomes the normal
        # pending batch of a live channel.
        self.peer.tool_pair_read(ack_token=first["batch_id"])
        st = self.store.read()
        self.assertEqual(3, st["seen"]["reviewer-agent"],
                         "(ii) seen jumps over the hole and the folded runs")
        after_poison = st["pending_reads"][self.peer.role]
        self.assertEqual([4], after_poison["seqs"],
                         "the letter after the fold drains as a normal batch")

    def test_two_holes_resolve_one_at_a_time(self) -> None:
        """(iii) two holes: the later run stays recorded until its own hole closes."""
        import read_delivery

        state = {"seen": {"opencode": 0}}
        read_delivery.create_pending_batch(state, "opencode", 0, 1, [7])
        pending = state["pending_reads"]["opencode"]
        # Two acked runs recorded, separated by a second hole.
        pending["acked_runs"] = [[1, 3], [4, 6]]
        # Closing the first hole folds only the first run.
        read_delivery._fold_acked_runs(state, "opencode", 1)
        self.assertEqual(3, state["seen"]["opencode"],
                         "seen lands at the end of the FIRST contiguous run")
        self.assertEqual([[4, 6]], pending["acked_runs"],
                         "the later run stays recorded for its own hole")
        # Closing the second hole folds the rest. The file-3 batch acks first
        # (its own acknowledge moves seen to 4); the fold then catches the run.
        read_delivery._fold_acked_runs(state, "opencode", 4)
        self.assertEqual(6, state["seen"]["opencode"])
        self.assertNotIn("acked_runs", pending)

    def test_digest_names_the_poison_with_path_and_cure(self) -> None:
        batch = self._batch_of("digest poison")
        inbox = self.store.mailbox_root / self.peer.role
        sorted(inbox.glob("*.json"))[0].write_text("not json", encoding="utf-8")
        self.peer.tool_pair_read()
        text = self.peer.tool_pair_digest()["digest"]
        self.assertIn("POISONED:", text)
        self.assertIn("cause:", text)
        self.assertIn("pairctl read-ack", text)

    def test_digest_prints_no_poison_for_a_healthy_batch(self) -> None:
        self._batch_of("healthy batch")
        text = self.peer.tool_pair_digest()["digest"]
        self.assertIn("PENDING READ", text)
        self.assertNotIn("POISONED", text)


    def test_state_pending_token_always_acks_consistency(self) -> None:
        """(a) Whatever pair_state calls pending, pair_read can ack - one truth.

        Round 101 live finding (seq 1477): a stale server answered two ways
        about one batch - pair_read refused the token pair_state still showed.
        On committed code these two surfaces must never disagree.
        """
        batch = self._batch_of("consistency letter")["pending_read"]
        st = self.peer.tool_pair_state()
        shown = st["pending_read"]["batch_id"]
        self.assertEqual(batch["batch_id"], shown)
        acked = self.peer.tool_pair_read(ack_token=shown)
        self.assertNotIn("ack_token", acked,
                         "a bare ack returns no new batch, no new token")
        st_after = self.peer.tool_pair_state()
        self.assertIsNone(st_after.get("pending_read"),
                          "(b) after an ack, no pending batch remains")
        self.assertEqual(0, st_after["unread"],
                          "(b) the acked letter no longer reads unread")

if __name__ == "__main__":
    unittest.main()
