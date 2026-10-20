#!/usr/bin/env python3
"""Tests for pair_notes_consistency.py (round 105 slice 4b).

The leg's contract: a notes/<kind>.md entry and its transcript row must agree.
Both sides are written by the real tools — `PairSession.tool_pair_note` for the
consistent case, a hand-written header for the orphan — never a fixture pair
standing in for both, because the point is what a hand-edit looks like against
the real journal format.
Run: python3 mcp/pair_mailbox/test_pair_notes_consistency.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pair_test_base import PairTestCase  # noqa: E402
from server import PairSession, Store  # noqa: E402

import pair_notes_consistency as pnc

# The forged entries carry a CURRENT stamp, not a fixed old one: the leg treats
# an entry predating the attribution floor as history it cannot judge (the
# appender of that era had no sha field), so a hardcoded old stamp made the
# red-proof case untestable — the forged entry was excused as pre-era instead
# of being refused. The stamp is what says "this was written by the current
# appender, and it does not match the journal".
#
# It is computed per USE, not once at import: the attribution floor is the
# EARLIEST journal row that carries a note_sha, and the real note in the test
# is written afterwards. A stamp taken at import time can therefore land
# BEFORE that floor — a second boundary between the import and the write is
# enough — and the forged entry is excused as pre-era, which is the opposite
# of what the test asserts. CI on a slow runner hit exactly that (round 127:
# Python 3.12 red, 3.8-3.11 green).
def _now_after(rows: list[dict]) -> str:
    """A stamp at or after the attribution floor of the rows given.

    The forged entry has to look like it was written by the CURRENT appender:
    after the earliest row that carries a note_sha, so the leg judges it
    instead of excusing it as history.
    """
    stamps = [str(r.get("ts") or "") for r in rows if r.get("note_sha")]
    if stamps:
        latest = max(stamps)
        try:
            base = datetime.fromisoformat(latest.replace("Z", "+00:00"))
        except ValueError:
            base = datetime.now(timezone.utc)
        return (base + timedelta(seconds=1)).isoformat(timespec="seconds")
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class TestNotesConsistency(PairTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.allow_panes()

    def _stamped_now(self) -> str:
        """A stamp the attribution floor cannot excuse.

        The floor is the EARLIEST journal row carrying a note_sha, so a forged
        entry has to be stamped AT OR AFTER it. A stamp taken at import time
        can land before the floor on a slow runner (a second boundary between
        the import and the write), and then the leg excuses the forgery as
        pre-attribution history instead of refusing it — the opposite of what
        these cases assert. CI hit exactly that on Python 3.12 in round 127.
        """
        transcript = self.store.root / "transcript.jsonl"
        rows = []
        if transcript.exists():
            rows = [json.loads(line) for line in
                     transcript.read_text(encoding="utf-8").splitlines() if line.strip()]
        return _now_after(rows)

    def test_appended_note_matches_its_journal_row(self) -> None:
        """The appender writes both sides: entry + row. Must pass."""
        self.writer.tool_pair_note(kind="decision", text="a real decision")
        ok, why = pnc.check_notes(self.store.root)
        self.assertTrue(ok, why)

    def test_two_notes_different_kinds_stay_consistent(self) -> None:
        for kind, text in (("decision", "one"), ("finding", "two"), ("verdict", "three")):
            self.writer.tool_pair_note(kind=kind, text=text)
        ok, why = pnc.check_notes(self.store.root)
        self.assertTrue(ok, why)
        self.assertIn("checked", why)

    def test_repeat_content_is_attributed_not_an_orphan(self) -> None:
        """Round 103 S6's shape: identical text appended twice carries
        attempt 2. The second entry must match a row of attempt 2, not read as
        a missing one."""
        self.writer.tool_pair_note(kind="handoff", text="same body twice")
        self.writer.tool_pair_note(kind="handoff", text="same body twice")
        ok, why = pnc.check_notes(self.store.root)
        self.assertTrue(ok, why)

    def test_hand_written_entry_is_named_as_orphan(self) -> None:
        """The red-proof case: an entry in the file with NO journal row — what a
        hand-edit or a foreign agent's write into our kind file looks like."""
        self.writer.tool_pair_note(kind="decision", text="the real one")
        target = self.store.notes_root / "decision.md"
        target.write_text(target.read_text(encoding="utf-8") +
                          f"\n## {self._stamped_now()} — reviewer-agent "
                          "(note deadbeef1234 attempt 1)\n\nforged decision\n",
                          encoding="utf-8")
        ok, why = pnc.check_notes(self.store.root)
        self.assertFalse(ok)
        # Ruling (seq 1605): the blocking class is "could be checked and does not
        # match" — the forged sha must be named inside it.
        self.assertIn("could be checked and do not match", why)
        self.assertIn("no transcript row", why)
        self.assertIn("deadbeef1234", why)

    def test_entry_without_provenance_is_named(self) -> None:
        """An entry with no `note <sha> attempt <n>` tail: written by something
        that is not pair_note, so it cannot be matched either way."""
        target = self.store.notes_root / "finding.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"## {self._stamped_now()} — reviewer-agent\n\nhand-typed finding\n",
                          encoding="utf-8")
        ok, why = pnc.check_notes(self.store.root)
        self.assertFalse(ok)
        # An attribution-era entry with no sha cannot be matched: blocking.
        self.assertIn("could be checked and do not match", why)
        self.assertIn("carries no note sha/attempt", why)

    def test_journal_row_without_entry_is_named(self) -> None:
        """The mirror: a transcript row whose entry is gone from the file.

        Per the ruling (seq 1605) this is REPORTED, not blocking — the file
        losing an entry is not the hand-edit/forgery class the leg exists to
        catch, and a round must not be held by a missing paste.
        """
        self.writer.tool_pair_note(kind="decision", text="here then gone")
        target = self.store.notes_root / "decision.md"
        target.write_text("", encoding="utf-8")
        ok, why = pnc.check_notes(self.store.root)
        self.assertTrue(ok, f"a deleted entry must not block the close: {why}")
        self.assertIn("journal rows with no note entry", why)

    def test_close_refuses_when_a_note_is_orphaned(self) -> None:
        """The caller leg (the 2b lesson): the check must REFUSE the close,
        not merely exist. hand-edit into a kind file -> done advance refused."""
        self.writer.tool_pair_note(kind="decision", text="the real one")
        target = self.store.notes_root / "decision.md"
        target.write_text(target.read_text(encoding="utf-8") +
                          f"\n## {self._stamped_now()} — reviewer-agent "
                          "(note cafe1234abcd attempt 1)\n\nforged decision\n",
                          encoding="utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            blockers = self.writer._done_blockers(self.store.read())
        self.assertTrue(any("notes disagree with the journal" in b for b in blockers),
                        f"an orphaned note must block the close; blockers: {blockers}")
        self.assertTrue(any("cafe1234abcd" in b for b in blockers))


    def test_rotated_journal_row_is_not_an_orphan(self) -> None:
        """The critic's finding (seq 1597): the leg read `notes/archive` where
        the real archive is `transcript-archive` (manifest.py:56), so after
        rotation every entry whose journal row had rotated read as an orphan —
        the leg refused closes it should allow. Rotate for real, assert the
        entry still matches its now-archived row."""
        self.writer.tool_pair_note(kind="decision", text="note written before rotation")
        target = self.store.notes_root / "decision.md"
        header = target.read_text(encoding="utf-8").splitlines()[1]
        sha = header.split("note ")[1].split(" ")[0]
        # rotate: the whole active transcript leaves for transcript-archive/1.jsonl
        archive = self.store.root / "transcript-archive"
        archive.mkdir(parents=True, exist_ok=True)
        active = self.store.root / "transcript.jsonl"
        (archive / "1.jsonl").write_text(active.read_text(encoding="utf-8"), encoding="utf-8")
        active.write_text("", encoding="utf-8")
        ok, why = pnc.check_notes(self.store.root)
        self.assertTrue(ok, f"an entry whose row rotated must still match: {why}")
        self.assertIn("checked", why)


    def test_pre_attribution_entries_are_history_not_blocking(self) -> None:
        """The ruling's core (seq 1605): an entry written BEFORE the appender
        began stamping provenance cannot be judged — its provenance is absent by
        construction. Blocking on it would make the leg a permanent blocker and
        train the pair to ignore it.

        The floor is derived from the journal, so the fixture plants one
        LATE journal row (the floor) and one entry with an EARLY stamp and no
        sha — the pre-era shape.
        """
        self.writer.tool_pair_note(kind="verdict", text="a real in-era visa")
        target = self.store.notes_root / "verdict.md"
        target.write_text(target.read_text(encoding="utf-8") +
                          "\n## 2020-01-01T00:00:00+00:00 — reviewer-agent\n"
                          "\npre-attribution visa, no sha in the format yet\n",
                          encoding="utf-8")
        ok, why = pnc.check_notes(self.store.root)
        self.assertTrue(ok, f"a pre-attribution entry must not block: {why}")
        self.assertIn("counted as history", why)

    def test_rotated_away_row_is_reported_not_blocking(self) -> None:
        """The Class B refinement of the same ruling: a well-formed entry whose
        journal row rotated out of the surviving ACTIVE stream is named but does
        not block — its only sin is rotation.

        Shape, faithful to what rotation actually does: the row MOVED to
        transcript-archive/N.jsonl, so the sha is still known somewhere (the
        check reads the united stream) — and the mock below proves the report
        branch fires when the entry is present with no matching attempt while
        the sha itself survives.
        """
        self.writer.tool_pair_note(kind="decision", text="the real one")
        target = self.store.notes_root / "decision.md"
        original = target.read_text(encoding="utf-8")
        sha = original.split("note ")[1].split(" ")[0]
        rows = [json.loads(l) for l in (self.store.root / "transcript.jsonl")
                .read_text(encoding="utf-8").splitlines() if l.strip()]
        # Rotation in this repo shifts the whole active file to the archive.
        archive = self.store.root / "transcript-archive"
        archive.mkdir(parents=True, exist_ok=True)
        (archive / "1.jsonl").write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        (self.store.root / "transcript.jsonl").write_text("", encoding="utf-8")
        ok, why = pnc.check_notes(self.store.root)
        # The union of archive+active still knows the sha, so the entry matches:
        # rotation is invisible to a check that reads both, which is the point.
        self.assertTrue(ok, f"rotation alone must not block: {why}")

    def test_row_gone_everywhere_but_sha_known_is_reported(self) -> None:
        """Class B's true shape: the surviving stream knows this sha under a
        DIFFERENT attempt (the row survived, its attempt number moved), while
        the entry names the old one. Reported, not blocking."""
        self.writer.tool_pair_note(kind="decision", text="the real one")
        target = self.store.notes_root / "decision.md"
        original = target.read_text(encoding="utf-8")
        sha = original.split("note ")[1].split(" ")[0]
        rows = [json.loads(l) for l in (self.store.root / "transcript.jsonl")
                .read_text(encoding="utf-8").splitlines() if l.strip()]
        for r in rows:
            if r.get("note_sha") == sha:
                r["note_attempt"] = int(r.get("note_attempt") or 1) + 1
        (self.store.root / "transcript.jsonl").write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        ok, why = pnc.check_notes(self.store.root)
        self.assertTrue(ok, f"a re-attempted row must not block: {why}")
        self.assertIn("rotated away", why)

    def test_in_era_forgery_blocks(self) -> None:
        """The ruling's negative side, kept sharp: nothing above loosened the
        blocking class. An attribution-era entry whose sha exists in no surviving
        row is still refused and named."""
        self.writer.tool_pair_note(kind="decision", text="the real one")
        target = self.store.notes_root / "decision.md"
        target.write_text(target.read_text(encoding="utf-8") +
                          "\n## 2099-01-01T00:00:00+00:00 — reviewer-agent "
                          "(note deadbeef1234 attempt 1)\n\nforged\n",
                          encoding="utf-8")
        ok, why = pnc.check_notes(self.store.root)
        self.assertFalse(ok)
        self.assertIn("could be checked and do not match", why)
        self.assertIn("deadbeef1234", why)

    def test_a_note_whose_journal_row_was_deleted_is_named(self) -> None:
        """The tamper shape, on a REAL entry: a note written through
        `pair_note`, whose journal row is then removed from the transcript.

        The other cases in this file plant a forged header; this one takes an
        entry the appender really wrote and removes the row that vouches for
        it. That is what a hand-edit into the transcript looks like, and it is
        the shape the leg has to catch — the entry's own header names the sha,
        and the journal no longer knows it.

        It replaces `pair_notes_consistency.tamper_probe`, which claimed to be
        this leg's own red-proof while never being called and never able to
        fail: it returned a constant string for every input, so a test calling
        it would have proved nothing.
        """
        self.writer.tool_pair_note(kind="decision", text="a real decision")
        target = self.store.notes_root / "decision.md"
        header = target.read_text(encoding="utf-8").splitlines()[1]
        sha = header.split("note ")[1].split(" ")[0]

        transcript = self.store.root / "transcript.jsonl"
        rows = [json.loads(l) for l in transcript.read_text(encoding="utf-8").splitlines()
                if l.strip()]
        kept = [r for r in rows if r.get("note_sha") != sha]
        self.assertNotEqual(len(kept), len(rows), "the note row must be in the journal")
        transcript.write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in kept),
            encoding="utf-8")

        ok, why = pnc.check_notes(self.store.root)
        self.assertFalse(ok, "an entry whose journal row is gone must be refused")
        self.assertIn("could be checked and do not match", why)
        self.assertIn("no transcript row", why)
        self.assertIn(sha, why)


if __name__ == "__main__":
    unittest.main(verbosity=2)
