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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pair_test_base import PairTestCase  # noqa: E402
from server import PairSession, Store  # noqa: E402

import pair_notes_consistency as pnc


class TestNotesConsistency(PairTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.allow_panes()

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
                          "\n## 2026-10-17T20:00:00+00:00 — reviewer-agent "
                          "(note deadbeef1234 attempt 1)\n\nforged decision\n",
                          encoding="utf-8")
        ok, why = pnc.check_notes(self.store.root)
        self.assertFalse(ok)
        self.assertIn("no journal row", why)
        self.assertIn("deadbeef1234", why)

    def test_entry_without_provenance_is_named(self) -> None:
        """An entry with no `note <sha> attempt <n>` tail: written by something
        that is not pair_note, so it cannot be matched either way."""
        target = self.store.notes_root / "finding.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("## 2026-10-17T20:00:00+00:00 — reviewer-agent\n\nhand-typed finding\n",
                          encoding="utf-8")
        ok, why = pnc.check_notes(self.store.root)
        self.assertFalse(ok)
        self.assertIn("no provable provenance", why)

    def test_journal_row_without_entry_is_named(self) -> None:
        """The mirror: a transcript row whose entry is gone from the file."""
        self.writer.tool_pair_note(kind="decision", text="here then gone")
        target = self.store.notes_root / "decision.md"
        target.write_text("", encoding="utf-8")
        ok, why = pnc.check_notes(self.store.root)
        self.assertFalse(ok)
        self.assertIn("journal rows with no note entry", why)

    def test_close_refuses_when_a_note_is_orphaned(self) -> None:
        """The caller leg (the 2b lesson): the check must REFUSE the close,
        not merely exist. hand-edit into a kind file -> done advance refused."""
        self.writer.tool_pair_note(kind="decision", text="the real one")
        target = self.store.notes_root / "decision.md"
        target.write_text(target.read_text(encoding="utf-8") +
                          "\n## 2026-10-17T20:00:00+00:00 — reviewer-agent "
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
