"""State-fingerprint tests for the `pair_state` cache.

One test file per source: `statecache.py` owns one thing — fingerprinting what the
full answer reads — checking here, never in `test_protocol.py` with the server.

The opposite of a false "unchanged" checks first: the fingerprint must move when
the answer moved. A miss that way returns the agent a stub with a truth that never
was, worse than an extra full answer.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pair_test_base import PairTestCase  # noqa: E402

import statecache  # noqa: E402


class TestCacheDisabled(unittest.TestCase):
    def test_enabled_when_env_is_absent(self) -> None:
        self.assertFalse(statecache.cache_disabled({}))

    def test_disabled_by_the_documented_variable(self) -> None:
        """A switch promised by a comment must truly switch off.

        Tests needing a live answer per call lean on it: with no it the closing-fact
        check would read the stub passing, checking nothing.
        """
        self.assertTrue(
            statecache.cache_disabled({statecache.CACHE_DISABLED_ENV: "1"})
        )

    def test_reads_the_real_environment_by_default(self) -> None:
        """The process env takes with no argument, never an empty dict."""
        original = os.environ.get(statecache.CACHE_DISABLED_ENV)
        os.environ[statecache.CACHE_DISABLED_ENV] = "1"
        try:
            self.assertTrue(statecache.cache_disabled())
        finally:
            if original is None:
                os.environ.pop(statecache.CACHE_DISABLED_ENV, None)
            else:
                os.environ[statecache.CACHE_DISABLED_ENV] = original


class TestFingerprintSeesEverythingTheAnswerReads(unittest.TestCase):
    def base_state(self) -> dict:
        return {
            "task": "t", "mode": "fix", "phase": "implement", "round": 1,
            "writer": "opencode", "evidence": [], "human_ok": False,
            "roles": {"opencode": {"pid": 1, "last_seen": "x"}},
        }

    def test_same_state_gives_same_fingerprint(self) -> None:
        state = self.base_state()
        self.assertEqual(
            statecache.fingerprint(state, obligation_names=[], inbox_names=[]),
            statecache.fingerprint(state, obligation_names=[], inbox_names=[]),
        )

    def test_updated_at_alone_does_not_change_it(self) -> None:
        """`updated_at` moves on every call alone.

        Inside the fingerprint it would void the cache: the stub would never return.
        """
        first = dict(self.base_state(), updated_at="2026-01-01T00:00:00+00:00")
        second = dict(first, updated_at="2026-01-01T00:00:01+00:00")
        self.assertEqual(statecache.fingerprint(first), statecache.fingerprint(second))

    def test_last_seen_alone_does_not_change_it(self) -> None:
        """`last_seen` is a registration trace too, not answer content."""
        first = {"roles": {"pi": {"pid": 5, "last_seen": "a"}}}
        second = {"roles": {"pi": {"pid": 5, "last_seen": "b"}}}
        self.assertEqual(statecache.fingerprint(first), statecache.fingerprint(second))

    def test_new_evidence_changes_it(self) -> None:
        """Exactly the case being fixed: evidence writes between calls.

        The old key never saw it, and the agent took a stub with no
        `evidence_this_round` — exactly what the `pair_state` call was for.
        """
        before = dict(self.base_state(), evidence=[])
        after = dict(self.base_state(), evidence=[{"kind": "test", "value": "1 passed"}])
        self.assertNotEqual(statecache.fingerprint(before), statecache.fingerprint(after))

    def test_human_ok_changes_it(self) -> None:
        before = dict(self.base_state(), human_ok=False)
        after = dict(self.base_state(), human_ok=True)
        self.assertNotEqual(statecache.fingerprint(before), statecache.fingerprint(after))

    def test_verdicts_change_it(self) -> None:
        before = dict(self.base_state(), verdicts=[])
        after = dict(self.base_state(), verdicts=[{"role": "pi"}])
        self.assertNotEqual(statecache.fingerprint(before), statecache.fingerprint(after))

    def test_patches_change_it(self) -> None:
        before = dict(self.base_state(), patches={})
        after = dict(self.base_state(), patches={"p1": {"applied": True}})
        self.assertNotEqual(statecache.fingerprint(before), statecache.fingerprint(after))

    def test_a_new_pid_changes_it(self) -> None:
        """A neighbour restart moves the answer, and the agent must see it."""
        before = {"roles": {"pi": {"pid": 1, "last_seen": "a"}}}
        after = {"roles": {"pi": {"pid": 2, "last_seen": "a"}}}
        self.assertNotEqual(statecache.fingerprint(before), statecache.fingerprint(after))

    def test_a_new_obligation_file_changes_it(self) -> None:
        """Obligations lie on disk, never in state.json.

        Had the fingerprint seen state only, an open obligation would move nothing —
        and the agent would read "nobody awaits anybody" about being awaited.
        """
        state = self.base_state()
        self.assertNotEqual(
            statecache.fingerprint(state, obligation_names=["pi.json"]),
            statecache.fingerprint(state, obligation_names=[]),
        )

    def test_a_new_inbox_file_changes_it(self) -> None:
        state = self.base_state()
        self.assertNotEqual(
            statecache.fingerprint(state, inbox_names=["1-pi.json"]),
            statecache.fingerprint(state, inbox_names=[]),
        )

    def test_a_new_peer_inbox_file_changes_it(self) -> None:
        """A foreign letter moves the fingerprint too: or the caller took `unchanged`
        with new unread."""
        state = self.base_state()
        self.assertNotEqual(
            statecache.fingerprint(state, peer_inbox_names={"pi": ["1-x.json"]}),
            statecache.fingerprint(state, peer_inbox_names={"pi": []}),
        )


    def test_obligation_order_does_not_change_it(self) -> None:
        """Name order is no content: two calls must never differ over it."""
        state = self.base_state()
        self.assertEqual(
            statecache.fingerprint(state, obligation_names=["a.json", "b.json"]),
            statecache.fingerprint(state, obligation_names=["b.json", "a.json"]),
        )


class TestGitSnapshotOnlyMattersWithVerdicts(unittest.TestCase):
    def test_snapshot_ignored_without_verdicts_in_audit(self) -> None:
        """Audit carries no delivery gate: with no verdicts no snapshot reads.

        In mode=fix the snapshot now also feeds the delivery floor (round 91,
        Trap 2: a push between two pair_state calls must move the key, or the
        stale \"nothing was pushed\" blocker survives behind unchanged:true).
        So the old \"no verdicts means no call\" holds only where delivery
        never applies — audit. In fix the call fires and the key must move.
        """
        state = {"evidence": [], "mode": "audit"}
        calls = []

        def snapshot():
            calls.append(1)
            return "head-abc"

        statecache.fingerprint(state, git_snapshot=snapshot)
        self.assertEqual(calls, [])

    def test_snapshot_runs_without_verdicts_in_fix(self) -> None:
        """Trap 2 regression: the working round (fix, no verdicts yet) still
        feeds the delivery floor into the fingerprint."""
        state = {"evidence": [], "mode": "fix"}
        calls = []

        def snapshot():
            calls.append(1)
            return "head-abc"

        before = statecache.fingerprint(state, git_snapshot=snapshot)
        self.assertEqual(calls, [1])

        def moved():
            return "head-xyz"

        after = statecache.fingerprint(state, git_snapshot=moved)
        self.assertNotEqual(before, after)

    def test_snapshot_included_when_verdicts_exist(self) -> None:
        """A verdict stands — its stale follows HEAD, and the fingerprint must see it."""
        state = {"verdicts": [{"role": "pi"}]}
        before = statecache.fingerprint(state, git_snapshot=lambda: "head-abc")
        after = statecache.fingerprint(state, git_snapshot=lambda: "head-xyz")
        self.assertNotEqual(before, after)

    def test_missing_snapshot_callable_is_not_an_error(self) -> None:
        """No snapshot and no verdicts — the fingerprint still counts."""
        self.assertTrue(statecache.fingerprint({"evidence": []}))


class TestUnchangedAnswer(unittest.TestCase):
    def test_shape_and_hash(self) -> None:
        answer = statecache.unchanged_answer("abc123")
        self.assertTrue(answer["unchanged"])
        self.assertEqual(answer["hash"], "abc123")

    def test_note_points_at_a_tool_that_works(self) -> None:
        """The stub refers to `pair_digest`, so it must stay callable.

        The `pair-work` skill instruction starts the round exactly there, and the
        cache refers there too: with no answering digest the savings promise those
        who never take them, and the agent silently leaves paying for a full
        `pair_state`.
        """
        note = statecache.unchanged_answer("h")["note"]
        self.assertIn("pair_digest", note)

        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from server import HANDLERS, tool_definitions

        advertised = {tool["name"] for tool in tool_definitions()}
        self.assertIn("pair_digest", advertised)
        self.assertTrue(callable(HANDLERS.get("pair_digest")),
                        "pair_digest declared but uncallable")


class TestCacheOverARealSession(PairTestCase):
    """The fingerprint on a live session, not a dict.

    The unit tests above prove the fingerprint reacts to the listed fields. These
    prove it reacts to what the agent sees between two `pair_state` calls in a live
    round. Exactly on them the old key fell: `obligations` and `evidence_this_round`
    never entered the fingerprint, and the second call returned a stub — an agent
    that wrote proof read its absence.

    Write authority takes for real: proof written with no it is no round evidence,
    and the check would pass on empty ground.
    """

    def setUp(self) -> None:
        super().setUp()
        self.writer.tool_pair_state()
        self.peer.tool_pair_state()
        self.writer.tool_pair_claim(reason="writer")
        self.advance_phases()

    def test_evidence_becomes_visible_on_the_next_call(self) -> None:
        """The agent must see the reason upfront, never spend a turn on a known refusal."""
        self.set_autonomy("semi")
        state = self.writer.tool_pair_state()
        self.assertTrue(state["closing_blocked_by"])
        self.assertTrue(state["awaiting_human_ok"])
        self.assertEqual(state["evidence_this_round"], [])

        self.record_test_evidence()

        state = self.writer.tool_pair_state()
        self.assertEqual(
            state["evidence_this_round"][0]["kind"], "test",
            "the stub returned: written proof never shows",
        )
        self.assertEqual(len(state["closing_blocked_by"]), 1,
                         "only the human word remains")

    def test_a_new_obligation_is_visible_to_both_sides(self) -> None:
        """Obligations lie on disk, and the fingerprint must see them.

        Or the letter author read "nobody awaits anybody" about just asking the
        neighbour for a check, sending a second letter into the same box.
        """
        self.writer.tool_pair_send(to="reviewer-agent", text="check it",
                                   expects_reply=True)

        mine = self.writer.tool_pair_state()["obligations"]
        self.assertEqual(1, len(mine["i_am_waiting_for"]))
        self.assertEqual("reviewer-agent", mine["i_am_waiting_for"][0]["to"])

        theirs = self.peer.tool_pair_state()["obligations"]
        self.assertEqual(1, len(theirs["they_wait_for_me"]))
        self.assertEqual([], theirs["i_am_waiting_for"])

    def test_a_repeated_call_with_nothing_changed_is_a_stub(self) -> None:
        """The cache itself must work, or it charges for what it never does.

        The shared fixture switches the cache off on purpose — the other suites
        check the "every call returns full state" contract. Here the contract runs
        reverse, so the cache switches on for the test, dropping back in tearDown
        as it was.

        Both sides check: unchanged state gives the short answer, changed the full.
        With no second half the test would pass on a cache that always stays silent.
        """
        os.environ.pop(statecache.CACHE_DISABLED_ENV, None)
        try:
            first = self.writer.tool_pair_state()
            self.assertNotIn("unchanged", first)

            second = self.writer.tool_pair_state()
            self.assertTrue(second.get("unchanged"), "the cache never fired on the unchanged")
            self.assertIn("pair_digest", second["note"])

            self.record_test_evidence()
            self.assertNotIn(
                "unchanged", self.writer.tool_pair_state(),
                "the cache answered stub past writing proof",
            )
        finally:
            os.environ[statecache.CACHE_DISABLED_ENV] = "1"

    def test_a_dead_peer_is_not_hidden_by_the_stub(self) -> None:
        """A neighbour death must pierce the stub, never hide behind it.

        `peers.alive` follows a live `os.kill(pid, 0)`, not the state number: the
        process dies outside, state never moves, the fingerprint matches — and the
        stub would lie that the neighbour listens. A live process proves (`sleep
        60`, then `kill`): before death alive=True, past it the stub instead of
        alive=False.
        """
        import subprocess

        os.environ.pop(statecache.CACHE_DISABLED_ENV, None)
        try:
            self.writer.tool_pair_claim(reason="writer")
            self.writer.tool_pair_state()
            self.peer.tool_pair_state()
            proc = subprocess.Popen(["sleep", "60"])
            try:
                self.store.mutate(
                    lambda s: s["roles"]["reviewer-agent"].__setitem__(
                        "pid", proc.pid))
                alive = self.writer.tool_pair_state()
                self.assertIn("reviewer-agent", alive["peers"])
                self.assertTrue(
                    self.writer.tool_pair_state().get("unchanged"),
                    "the cache never fired on the unchanged",
                )
                proc.kill()
                proc.wait()
                reread = self.writer.tool_pair_state()
                self.assertNotIn("unchanged", reread,
                                 "the cache hid a neighbour death behind the stub")
                self.assertFalse(reread["peers"]["reviewer-agent"]["alive"])
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
        finally:
            os.environ[statecache.CACHE_DISABLED_ENV] = "1"


    def test_a_new_letter_is_not_hidden_by_the_stub(self) -> None:
        """A new letter to the neighbour must pierce the stub, never hide behind it.

        The fingerprint sees the box by file names, reading by `seen` in state.
        A live call checks: a stub on silence, a full answer past the letter, a full
        answer past reading. With no this the pair fell back to blind polling: an
        agent with cache never noticed being written to.
        """
        os.environ.pop(statecache.CACHE_DISABLED_ENV, None)
        try:
            self.peer.tool_pair_state()
            self.assertTrue(
                self.peer.tool_pair_state().get("unchanged"),
                "the cache never fired on the unchanged",
            )

            self.writer.tool_pair_send(
                to="reviewer-agent", text="look here", expects_reply=False)
            after = self.peer.tool_pair_state()
            self.assertNotIn("unchanged", after,
                             "the cache hid a new letter behind the stub")
            self.assertEqual(after.get("unread"), 1)

            self.peer.tool_pair_read(limit=10)
            reread = self.peer.tool_pair_state()
            self.assertNotIn("unchanged", reread,
                             "the cache hid reading behind the stub")
            self.assertEqual(reread.get("unread"), 0)
        finally:
            os.environ[statecache.CACHE_DISABLED_ENV] = "1"



if __name__ == "__main__":
    unittest.main(verbosity=2)