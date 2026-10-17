"""Machine gate plan → implement: file, visa, exemptions, self-visa.

One test file per source: `plan_gate.py` -> `test_plan_gate.py`
(the PairTestCase base gives a live two-role pair on state).
"""
from __future__ import annotations

import json
import time
import unittest
from pathlib import Path

from pair_test_base import PairTestCase
import plan_gate


def write_plan(state_dir: Path, round_number: int, sections: tuple = ("Goal", "Invariants", "Acceptance")) -> None:
    body = [f"round: {round_number}", ""]
    for section in sections:
        body += [f"## {section}", "text", ""]
    (state_dir / plan_gate.PLAN_FILE).write_text("\n".join(body), encoding="utf-8")


def write_ack(note_dir: Path, role: str, round_number: int, at: float | None = None) -> None:
    note_dir.mkdir(parents=True, exist_ok=True)
    stamp = at if at is not None else time.time()
    header = ("## 2026-10-11T18:00:00+00:00 — " + role + "\n") if at is None else None
    if header is None:
        from datetime import datetime, timezone
        header = ("## " + datetime.fromtimestamp(stamp, tz=timezone.utc).isoformat()
                  + " — " + role + "\n")
    (note_dir / "verdict.md").write_text(
        header + f"\nplan-ack round {round_number}\n", encoding="utf-8")
    # mtime is the truth source for "no earlier than plan": move the note forward
    import os
    os.utime(note_dir / "verdict.md", (stamp, stamp))


class TestGateCases(PairTestCase):
    """The gate wire through the real tool_pair_advance."""

    def setUp(self) -> None:
        super().setUp()
        self.writer.tool_pair_claim(reason="writer")

    def advance_to_plan(self) -> None:
        self.writer.tool_pair_advance("plan")

    # --- round 104 S2: the why must name the actual state, not collapse three ----

    def test_why_names_absence_not_staleness(self) -> None:
        """(a) no ack note at all is its own cause, and says so."""
        self.advance_to_plan()
        write_plan(self.store.root, 1)
        error = self.assertRefused(self.writer.tool_pair_advance, "implement")
        text = str(error)
        self.assertIn("no critic note", text)
        self.assertNotIn("predates", text,
                         "\"nothing\" and \"something old\" are different states")

    def test_why_names_wrong_round_ack(self) -> None:
        """(b) an ack for another round is its own cause."""
        self.advance_to_plan()
        write_plan(self.store.root, 1)
        write_ack(self.store.notes_root, "opencode", 99)
        error = self.assertRefused(self.writer.tool_pair_advance, "implement")
        text = str(error)
        self.assertIn("a critic visa exists", text)
        self.assertIn("round 99", text)
        self.assertIn("not 1", text)
        self.assertNotIn("no critic note with this round plan-ack", text)

    def test_why_names_stale_ack_not_absence(self) -> None:
        """(c) THIS round's ack that predates the current plan.

        The live failure (peer's own, round 101): the verdict printed "no critic
        note with this round plan-ack" while the note existed and named the
        right round — it was simply older than the plan. Three states, one
        sentence, and the wrong sentence told the writer to write a visa that
        already stood. `predates`/`stale` must appear.
        """
        self.advance_to_plan()
        write_plan(self.store.root, 1)
        write_ack(self.store.notes_root, "opencode", 1)
        # make the PLAN newer than the ack (mtime is the truth source)
        import os
        now = time.time()
        os.utime(self.store.root / plan_gate.PLAN_FILE, (now + 60, now + 60))
        error = self.assertRefused(self.writer.tool_pair_advance, "implement")
        text = str(error)
        self.assertTrue("predates" in text or "stale" in text,
                        f"stale ack must be named as stale, got: {text}")
        self.assertNotIn("no critic note with this round plan-ack", text)

    def test_no_plan_refuses(self) -> None:
        self.advance_to_plan()
        error = self.assertRefused(self.writer.tool_pair_advance, "implement")
        self.assertIn("unreadable", str(error))

    def test_wrong_round_marker_refuses(self) -> None:
        self.advance_to_plan()
        write_plan(self.store.root, 42)
        error = self.assertRefused(self.writer.tool_pair_advance, "implement")
        self.assertIn("round: 42", str(error))

    def test_missing_sections_refuse(self) -> None:
        self.advance_to_plan()
        write_plan(self.store.root, 1, sections=("Goal",))
        error = self.assertRefused(self.writer.tool_pair_advance, "implement")
        self.assertIn("invariants", str(error))

    def test_plan_plus_critic_ack_passes(self) -> None:
        self.advance_to_plan()
        write_plan(self.store.root, 1)
        write_ack(self.store.root / "notes", "reviewer-agent", 1, at=time.time() + 1)
        result = self.writer.tool_pair_advance("implement")
        self.assertEqual("implement", result["phase"])

    def test_visa_written_by_pair_note_is_readable(self) -> None:
        """Round 104/105: the gate must read what its own writer writes.

        `pair_note` has appended entries as `## <ts> — <role> (note <sha> attempt N)`
        since round 103 S6's attribution change, while NOTE_ENTRY demanded the bare
        `## <ts> — <role>` shape. Every tool-written visa was therefore invisible:
        the gate named a round-92 visa on today's round and 31 records in .pair/notes
        stayed unread. RED before the cure (critic_ack -> "no critic note"), GREEN
        after (ack: True, role, stamp). Derived through the tool's own appender, not
        a hand-written fixture — the fixture is what hid the defect for 12 green tests.
        """
        self.advance_to_plan()
        self.store.mutate(lambda st: st.__setitem__("round", 1))
        write_plan(self.store.root, 1)
        # The note header stamps seconds, plan.md mtime carries microseconds:
        # written in the same second, the truncated stamp reads as older. Push
        # the plan a second into the past so the comparison is about the shape,
        # not about a rounding accident.
        import os
        plan_path = self.store.root / plan_gate.PLAN_FILE
        stamped = os.stat(plan_path).st_mtime - 2
        os.utime(plan_path, (stamped, stamped))
        self.peer.tool_pair_note(kind="verdict", text="plan-ack round 1")
        found = plan_gate.critic_ack(self.store.root, 1, "writer-agent", not_before=None)
        self.assertTrue(found["ack"], f"the tool's own visa must be readable: {found}")
        self.assertEqual("reviewer-agent", found["role"])
        self.assertEqual("implement", self.writer.tool_pair_advance("implement")["phase"])

    def test_self_ack_does_not_count(self) -> None:
        """A self-visa from the writer bypasses the gate: the author role is checked."""
        self.advance_to_plan()
        write_plan(self.store.root, 1)
        write_ack(self.store.root / "notes", "writer-agent", 1, at=time.time() + 1)
        error = self.assertRefused(self.writer.tool_pair_advance, "implement")
        self.assertIn("window", str(error))

    def test_timeout_passes_without_open_requests(self) -> None:
        self.advance_to_plan()
        write_plan(self.store.root, 1)
        hours = plan_gate._ack_timeout_hours(self.store.root)
        stale = time.time() - hours * 3600 - 60
        import os
        os.utime(self.store.root / plan_gate.PLAN_FILE, (stale, stale))
        self.assertEqual("implement", self.writer.tool_pair_advance("implement")["phase"])

    def test_timeout_blocked_while_request_open(self) -> None:
        """An open writer request never lets the remark lapse unnoticed."""
        self.advance_to_plan()
        write_plan(self.store.root, 1)
        import os
        stale = time.time() - 25 * 3600
        os.utime(self.store.root / plan_gate.PLAN_FILE, (stale, stale))
        self.peer.tool_pair_send("writer-agent", "plan objection", expects_reply=False)
        self.writer.tool_pair_send("reviewer-agent", "approve the plan", expects_reply=True)
        error = self.assertRefused(self.writer.tool_pair_advance, "implement")
        self.assertIn("request", str(error))

    def test_reopen_is_exempt(self) -> None:
        """done → implement (finding reopen) would stall with no plan: self-lock is
        exactly the risk the exempt exists for."""
        self.ready_and_close()
        self.writer.tool_pair_advance("implement")
        self.assertEqual("implement", self.store.read()["phase"])

    def test_audit_needs_artifact_only(self) -> None:
        self.writer.tool_pair_advance("plan")
        self.store.mutate(lambda st: st.update(mode="audit"))
        write_plan(self.store.root, 1)
        self.assertEqual("implement", self.writer.tool_pair_advance("implement")["phase"])

    def ready_and_close(self) -> None:
        write_plan(self.store.root, self.store.read()["round"])
        write_ack(self.store.root / "notes", "reviewer-agent",
                  self.store.read()["round"], at=time.time() + 1)
        for phase in ("plan", "implement", "verify"):
            self.writer.tool_pair_advance(phase)
        self.record_test_evidence()
        self.writer.tool_pair_advance("done")


if __name__ == "__main__":
    unittest.main(verbosity=2)
