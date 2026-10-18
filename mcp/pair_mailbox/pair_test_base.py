"""The shared base for the pair-mailbox test suites.

This is NO test file but a fixture: it maps to no source, and calling it a third
test file would read wrong. It lives apart from `test_protocol.py` because past
splitting `server.py` the suites grew many sharing one base: a raised pair state
in a temp dir, two roles over it, plus helpers that would repeat per file line by
line.

The "one test file per source" rule never breaks here: the base checks no
behaviour, only readying the ground. Checks live in files named after the sources
they check.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from server import PairSession, ProtocolError, Store

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SERVER = Path(__file__).resolve().parent / "server.py"
PAIRCHAT = REPO_ROOT / "bin" / "pairchat"


class PairTestCase(unittest.TestCase):
    def allow_panes(self) -> None:
        """Count any pane ours, for the test run.

        Tests checking delivery, not addressing, need it: there `_register` must
        never refuse the pane as unproven, or the test falls before reaching its
        subject.

        Both questions stub — "is the pane ours" and "is the project foreign" —
        because `_register` asks the second, `pair_deliver` the first. Stubbing one
        means checking the wrong branch: exactly what happened when the second
        check appeared with the stub still on the first.
        """
        import composer

        real_foreign = composer.pane_is_foreign
        real_belongs = composer.pane_belongs_to_project
        composer.pane_is_foreign = lambda tmux, pane, root: (False, "")
        composer.pane_belongs_to_project = lambda tmux, pane, root: (True, "")
        self.addCleanup(setattr, composer, "pane_is_foreign", real_foreign)
        self.addCleanup(setattr, composer, "pane_belongs_to_project", real_belongs)

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "state"
        # Round 115 S4: this setUp BOOTSTRAPS a fresh state dir, which is exactly
        # the one lawful case for `allow_create=True` (bin/pairup does the same in
        # shell before any Store exists). Everything else in the tree reads an
        # existing state, so the default `allow_create=False` keeps the loud-fail
        # guard that turned a wrong root into two hours of phantom diagnosis.
        self.store = Store(self.root, allow_create=True)
        self.writer = PairSession("writer-agent", self.store)
        self.peer = PairSession("reviewer-agent", self.store)
        self._orig_tmux_pane = os.environ.pop("TMUX_PANE", None)
        # Remembered BEFORE the env clears: the tests below check pane_exists against the
        # real pane, while setUp drops the variable so suite roles never spam the operator
        # window (F-08). With no this remembering such a check would always skip — never run.
        # check would always skip — it would never run.
        self._pane_at_startup = self._orig_tmux_pane
        self._orig_disable_ring = os.environ.get("PAIR_DISABLE_RING")
        os.environ["PAIR_DISABLE_RING"] = "1"
        # The state cache in tool_pair_state returns a stub with no obligations and no
        # evidence_this_round on the second straight call. Tests check the "every call returns
        # full state" contract, so the cache switches off here (the server reads
        # PAIR_DISABLE_STATE_CACHE — see tool_pair_state).
        self._orig_disable_state_cache = os.environ.get("PAIR_DISABLE_STATE_CACHE")
        os.environ["PAIR_DISABLE_STATE_CACHE"] = "1"
        # ancestry addressing switches off BEFORE registering: extra self-address records
        # must never land in fill (287ff1d).
        os.environ["PAIR_DISABLE_ANCESTRY_PANE"] = "1"
        # Registering BEFORE mail runs mutate-merge, not tool_pair_state: the latter
        # rewrites the record whole dropping fields the test sets later.
        self.store.mutate(lambda s: s.setdefault("roles", {}).setdefault(
            "reviewer-agent", {}))
        # The default intent for the existing test suites
        self.write_intent(1)

    def tearDown(self) -> None:
        self._tmp.cleanup()
        if self._orig_tmux_pane is not None:
            os.environ["TMUX_PANE"] = self._orig_tmux_pane
        else:
            os.environ.pop("TMUX_PANE", None)
        if self._orig_disable_ring is not None:
            os.environ["PAIR_DISABLE_RING"] = self._orig_disable_ring
        else:
            os.environ.pop("PAIR_DISABLE_RING", None)
        if self._orig_disable_state_cache is not None:
            os.environ["PAIR_DISABLE_STATE_CACHE"] = self._orig_disable_state_cache
        else:
            os.environ.pop("PAIR_DISABLE_STATE_CACHE", None)
        os.environ.pop("PAIR_DISABLE_ANCESTRY_PANE", None)

    def assertRefused(self, callable_, *args, contains: str = "", **kwargs):
        with self.assertRaises(ProtocolError) as caught:
            callable_(*args, **kwargs)
        if contains:
            self.assertIn(contains, str(caught.exception).lower())
        return caught.exception

    def record_test_evidence(self, value: str = "117 passed in 6.2s (exit 0)", ok: bool = True,
                             task: int | None = None) -> None:
        """Record what a closing round needs: a machine-produced fact for THIS round."""
        self.writer.tool_pair_evidence(kind="test", value=value, ok=ok, task=task)
        if ok:
            # [R82] A real pair runs the gates too before closing: with no
            # writer-ground-truth done_blockers holds the round (the R81 incident).
            self.record_ground_truth()

    def record_ground_truth(self, value: str = "PASS anti-mock 0 blockers", ok: bool = True) -> None:
        "[R82] A live gate run: a real pair writes it before closing."
        self.writer.tool_pair_evidence(kind="ground-truth", value=value, ok=ok)


    def human_ok(self) -> None:
        """The one word a human says at a semi checkpoint, without leaving Python."""
        self.store.mutate(lambda state: state.setdefault("human_ok", []).append(
            {"round": int(state.get("round", 0)), "at": "now", "phase": state.get("phase")}))

    def close_round(self, value: str = "117 passed in 6.2s (exit 0)") -> dict:
        """Walk the phases and close, satisfying the gate the way a real pair would."""
        self.advance_phases()
        self.record_test_evidence(value)
        self.human_ok()
        return self.writer.tool_pair_advance(phase="done")

    def write_intent(self, round_number: int) -> None:
        "An intent artifact for the scoping → plan gate (round 57)."
        import intent_gate
        body = [f"round: {round_number}", "",
                "## Problem", "test bounds", "",
                "## Scope", "the test expected result", "",
                "## Outcome", "the test expected result", ""]
        (self.store.root / intent_gate.INTENT_FILE).write_text("\n".join(body), encoding="utf-8")

    def write_plan(self, round_number: int) -> None:
        """A plan artifact for the machine plan → implement gate (manifest 53).

        The fixture writes the CURRENT round plan: the gate matches the `round: N`
        marker against the round number, and a past-round plan would refuse —
        exactly the stale-artifact guard the gate adds.
        """
        import plan_gate
        body = [f"round: {round_number}", "",
                "## Goal", "test", "",
                "## Invariants", "test", "",
                "## Acceptance", "test", ""]
        (self.store.root / plan_gate.PLAN_FILE).write_text("\n".join(body), encoding="utf-8")
        # The gate reads not_before from the plan file's mtime, which carries
        # MICROSECONDS, while a note entry stamps whole seconds — a visa written
        # in the same second truncates older and is refused as "predates the
        # current plan". Backdate the plan so the visa it follows is unambiguously
        # newer. Same fix as the NOTE_ENTRY cure (round 104 slice 5).
        import os
        import time
        plan = self.store.root / plan_gate.PLAN_FILE
        stamp = time.time() - 2
        os.utime(plan, (stamp, stamp))

    def ack_plan(self, round_number: int) -> None:
        """The critic visa: a verdict note with a plan-ack round N line.

        The note mtime moves past the plan: the gate demands the visa no earlier
        than the plan (a past-round note never counts).
        """
        import os
        import time
        # Through the real appender, never a hand-shaped header: round 105 slice
        # 4b cross-checks notes/<kind>.md against the journal, and a helper
        # writing a lookalike — or a header whose sha has no journal row — gets
        # the suite refusing to close. Both shapes already bit, measured.
        session = self._peer_session()
        session.tool_pair_note(kind="verdict", text=f"plan-ack round {round_number}")
        note = self.store.root / "notes" / "verdict.md"
        if note.exists():
            now = time.time() + 1
            os.utime(note, (now, now))

    def _peer_session(self) -> Any:
        """The critic's own session, so the visa carries its role, not ours."""
        if not hasattr(self, "_peer_pair_session"):
            from server import PairSession
            self._peer_pair_session = PairSession("reviewer-agent", self.store)
        return self._peer_pair_session

    def advance_phases(self, phases: tuple = ("plan", "implement", "verify")) -> None:
        """Walk the phases as a real pair walks them: with a plan and a visa.

        Before the plan→implement gate the walk ran free, and fixtures merely
        walked phases. Now the move needs an artifact — the helper writes it for
        the pair, so tests check their subject, not the gate.

        The server freshness gate mutes here on purpose: it compares source mtimes
        against the process start, and any file edit during the run (or a fresh
        checkout) drops phase movement with ProtocolError — the seq-1207 flake,
        ~1 in 3 runs. The gate itself stays live-covered in test_server_freshness
        (refusal paths); here the test subject checks, not freshness.
        """
        import server_freshness
        orig = server_freshness.assert_server_fresh
        server_freshness.assert_server_fresh = lambda **kwargs: None
        try:
            for phase in phases:
                round_number = int(self.store.read().get("round", 0))
                if phase == "plan":
                    self.write_intent(round_number)
                elif phase == "implement":
                    self.write_plan(round_number)
                    self.ack_plan(round_number)
                self.writer.tool_pair_advance(phase)
        finally:
            server_freshness.assert_server_fresh = orig

    def set_autonomy(self, level: str) -> None:
        """Declare the level explicit.

        This helper lived inside one class, with six tests elsewhere silently
        leaning on the default — the level contract pinned by none of them, so a
        default change broke tests unrelated to the default.
        """
        self.store.mutate(lambda state: state.__setitem__("autonomy", level))