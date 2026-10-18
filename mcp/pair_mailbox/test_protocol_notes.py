#!/usr/bin/env python3
"""Protocol tests: notes, transcript window and their rotation."""

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


class TestPhasesAndNotes(PairTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.writer.tool_pair_state()
        self.peer.tool_pair_state()
        self.writer.tool_pair_claim()

    def test_legal_and_illegal_transitions(self) -> None:
        self.writer.tool_pair_advance("plan")
        self.assertEqual(self.store.read()["phase"], "plan")
        self.assertRefused(self.writer.tool_pair_advance, "done", contains="cannot move")
        self.write_plan(int(self.store.read().get("round", 0)))
        self.ack_plan(int(self.store.read().get("round", 0)))
        self.writer.tool_pair_advance("implement")
        self.writer.tool_pair_advance("verify")
        self.record_test_evidence()
        self.human_ok()
        self.writer.tool_pair_advance("done")

    def test_non_writer_cannot_advance_phase(self) -> None:
        self.assertRefused(self.peer.tool_pair_advance, "plan", contains="only the writer")

    def test_unknown_phase_is_refused(self) -> None:
        self.assertRefused(self.writer.tool_pair_advance, "shipping", contains="unknown phase")

    def test_notes_accumulate_per_kind(self) -> None:
        self.writer.tool_pair_note(kind="plan", text="step one: read the config loader")
        self.peer.tool_pair_note(kind="finding", text="config loader swallows the empty case")
        self.writer.tool_pair_note(kind="plan", text="step two: add the regression test")

        plan = (self.store.notes_root / "plan.md").read_text(encoding="utf-8")
        self.assertIn("step one", plan)
        self.assertIn("step two", plan)
        self.assertIn("config loader swallows", (self.store.notes_root / "finding.md").read_text(encoding="utf-8"))

    def test_note_kind_must_be_a_slug(self) -> None:
        self.assertRefused(self.writer.tool_pair_note, kind="../escape", text="x", contains="slug")
        self.assertRefused(self.writer.tool_pair_note, kind="plan", text="", contains="required")

    def test_note_row_carries_sha_and_attempt(self) -> None:
        """Round 103 S6: attribution on the note row. Five identical blocks
        landed in handoff.md at one ts and no field could say whether one call
        appended five times or five calls ran inside a second."""
        self.writer.tool_pair_note(kind="handoff", text="the same body\n")
        self.writer.tool_pair_note(kind="handoff", text="the same body")
        rows = [r for r in self.store.read_transcript(limit=20)
                if r.get("kind") == "note" and r.get("note_kind") == "handoff"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].get("note_sha"), rows[1].get("note_sha"),
                         "trailing whitespace must not change the sha")
        self.assertEqual(rows[0].get("note_attempt"), 1)
        self.assertEqual(rows[1].get("note_attempt"), 2,
                         "the repeat is counted, not silently swallowed")
        plan = (self.store.notes_root / "handoff.md").read_text(encoding="utf-8")
        self.assertIn("attempt 2", plan, "the file carries the attempt marker")

    def test_whitespace_only_difference_counts_as_repeat(self) -> None:
        """Peer amendment 2 (member of the class): blocks 1-4 sha-equal len
        1204, block 5 len 1203 — one trailing newline apart; exact equality
        refuses four and misses the fifth."""
        self.writer.tool_pair_note(kind="handoff", text="block body\n\n")
        self.writer.tool_pair_note(kind="handoff", text="block body\n")
        self.writer.tool_pair_note(kind="handoff", text="block body")  # no newline
        rows = [r for r in self.store.read_transcript(limit=20)
                if r.get("kind") == "note" and r.get("note_kind") == "handoff"]
        shas = {r.get("note_sha") for r in rows}
        self.assertEqual(len(shas), 1, "all three normalize to one sha")
        self.assertEqual(rows[-1].get("note_attempt"), 3)

    # --- round 104 S3: a verdict note does not owe the writer an answer -------

    def _open_obligations(self) -> list[dict]:
        try:
            return [json.loads(p.read_text(encoding="utf-8"))
                    for p in self.store.obligations_root.glob("*.json")]
        except OSError:
            return []

    def test_an_approve_note_opens_no_review_queue(self) -> None:
        """A critic's FINAL VERDICT — approve — is not a request for an answer.

        server.py:3478 opens an obligation for every kind that starts with
        "verdict", including the final approve and the review-ack. Live case
        (peer, round 103/104): the approve left pending_reviews=1 and the round
        could not close until the writer posted ANOTHER note — the queue held
        the round hostage over a completed review.
        """
        self.peer.tool_pair_note(kind="verdict", text="verdict-approve 9d2ca75 — fine as is")
        self.assertEqual(self._open_obligations(), [],
                         "a completed review owes nobody an answer")

    def test_a_review_ack_note_opens_no_review_queue(self) -> None:
        """A review-ack is a courtesy, not a request (peer's own live case)."""
        self.peer.tool_pair_note(
            kind="verdict-round104-s1",
            text="S1 reviewed and accepted; no objection")
        self.assertEqual(self._open_obligations(), [])

    def test_a_needs_work_note_still_opens_the_queue(self) -> None:
        """The opposite invariant: a real review REQUEST still creates work.

        A round may not close over an unread needs-work, so the obligation for
        that case must survive — the fix removes the flood, not the signal.
        """
        self.peer.tool_pair_send(to="writer-agent", expects_reply=True,
                                 text="NEEDS WORK on 9d2ca75: the why still collapses one state")
        opened = self._open_obligations()
        self.assertEqual(len(opened), 1)
        self.assertEqual(opened[0]["to"], "writer-agent")



class TestNoteTailReads(PairTestCase):
    """Notes never read whole: neither with a file tool, nor less.

        Measured live: `verdict.md` grew to 630 lines, about 19 thousand tokens per
        ONE read. `pair_note` owned no second side — reading — with no bounded form at
        all, so the only way to learn a note ran through reading it whole. One new tool
        costs ~143 tokens a query, paying off in ~100 queries and preventing one such read.
        """

    def _seed(self, kind: str, entries: int = 5, body: str = "content line") -> Path:
        path = self.store.notes_root / f"{kind}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            for i in range(entries):
                fh.write(f"\n## 2026-10-05T10:{i:02d}:00+00:00 — agy\n\n{i} {body}\n")
        return path

    def test_index_lists_notes_by_size_without_reading_them(self) -> None:
        self._seed("small", entries=2)
        self._seed("big", entries=40, body="x" * 200)
        idx = self.writer.tool_pair_note_index()
        kinds = [n["kind"] for n in idx["notes"]]
        self.assertIn("big", kinds)
        self.assertIn("small", kinds)
        self.assertEqual([n["bytes"] for n in idx["notes"]],
                         sorted((n["bytes"] for n in idx["notes"]), reverse=True),
                         "index sorts by size: the priciest shows first")
        big = [n for n in idx["notes"] if n["kind"] == "big"][0]
        self.assertEqual(big["entries"], 40)

    def test_read_returns_the_tail_and_says_it_was_truncated(self) -> None:
        self._seed("verdict", entries=30)
        res = self.writer.tool_pair_note_read(kind="verdict", tail_lines=10)
        self.assertTrue(res["truncated"])
        self.assertLessEqual(res["returned_lines"], 12)
        # 120 lines in the file minus the service blank before the first record.
        self.assertEqual(res["total_lines"], 119)
        self.assertEqual(res["file_lines"], 120)

    def test_a_short_note_is_not_truncated(self) -> None:
        self._seed("short", entries=3)
        res = self.writer.tool_pair_note_read(kind="short", tail_lines=100)
        self.assertFalse(res["truncated"])
        self.assertEqual(res["returned_lines"], res["total_lines"])

    def test_tail_starts_on_an_entry_boundary(self) -> None:
        """Half a record beats missing record: taken for whole.

        Checked is not "every line a header" — a record body is lawful — but what
        really guarantees: at ANY tail size the tail starts on a record edge and is
        an exact file suffix. Exactly this proves the count runs on record edges, not
        line numbers.

        The record itself differs by size — at a small size the fallback to the last
        whole record fires — and that is right: one whole record beats empty text or
        its half.
        """
        self._seed("verdict", entries=30)
        whole = (self.store.notes_root / "verdict.md").read_text(encoding="utf-8")
        for size in (1, 2, 5, 7, 15, 40, 500):
            res = self.writer.tool_pair_note_read(kind="verdict", tail_lines=size)
            lines = res["text"].splitlines()
            self.assertTrue(lines, f"tail at size {size} empty")
            self.assertTrue(lines[0].startswith("## "),
                            f"size {size}: tail started at {lines[0]!r}")
            self.assertIn(res["text"], whole,
                          f"size {size}: tail is not a file suffix")

    def test_tail_lines_is_capped(self) -> None:
        """A ceiling is mandatory, or the argument turns into a tome itself."""
        self._seed("verdict", entries=400)
        res = self.writer.tool_pair_note_read(kind="verdict", tail_lines=10 ** 6)
        self.assertLessEqual(res["returned_lines"], NOTE_READ_MAX + 3)
        by_limit = self.writer.tool_pair_note_read(kind="verdict", limit=10 ** 6)
        self.assertEqual(by_limit["text"], res["text"])

    def test_bad_arguments_are_refused(self) -> None:
        self._seed("verdict", entries=2)
        self.assertRefused(lambda: self.writer.tool_pair_note_read(kind="../escape"))
        self.assertRefused(lambda: self.writer.tool_pair_note_read(kind="verdict", tail_lines=0))
        self.assertRefused(lambda: self.writer.tool_pair_note_read(kind="verdict", limit=0))
        self.assertRefused(lambda: self.writer.tool_pair_note_read(kind="no-such"))



class TestNoteRotation(PairTestCase):
    """The growth ceiling: a note file must never grow without bound.

        Historic fix: the first version picked the count iteratively and hung
        forever — past the record-edge cut the next record always landed at index 0,
        the counter never grew. A big-note test found it, not review: at 200 records
        the loop never finished. Now counting back from the file length, milliseconds
        on that size.
        """

    def _seed(self, entries: int) -> Path:
        path = self.store.notes_root / "verdict.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            for i in range(entries):
                fh.write(f"\n## 2026-10-05T10:{i % 60:02d}:00+00:00 — agy\n\nverdict {i}\n")
        return path

    def test_rotation_keeps_the_file_under_the_cap(self) -> None:
        path = self._seed(200)
        self.assertGreater(len(path.read_text().splitlines()), NOTE_LINE_CAP)
        self.writer._rotate_note("verdict", path)
        lines = path.read_text().splitlines()
        self.assertLessEqual(len(lines), NOTE_LINE_CAP + 3,
                             "the tail must never outgrow the header ceiling")

    def test_rotation_loses_no_entries(self) -> None:
        path = self._seed(200)
        before = path.read_text()
        self.writer._rotate_note("verdict", path)
        archives = sorted((self.store.notes_root / "archive").glob("verdict-*.md"))
        self.assertTrue(archives, "old records must leave somewhere, never vanish")
        combined = before + "".join(a.read_text() for a in archives)
        for i in range(200):
            self.assertIn(f"verdict {i}", combined, f"record {i} lost")

    def test_rotation_leaves_a_pointer_to_the_archive(self) -> None:
        path = self._seed(200)
        self.writer._rotate_note("verdict", path)
        head = path.read_text().splitlines()[0]
        self.assertIn("archive/", head, "an archive pointer is mandatory, or the loss stays silent")

    def test_rotation_is_recorded_in_the_transcript(self) -> None:
        path = self._seed(200)
        self.writer._rotate_note("verdict", path)
        kinds = [e.get("kind") for e in self.store.read_transcript(limit=0)]
        self.assertIn("note_rotated", kinds)

    def test_a_second_rotation_terminates_and_is_idempotent(self) -> None:
        """A hang regression: two passes in a row must finish."""
        path = self._seed(200)
        self.writer._rotate_note("verdict", path)
        first = len(path.read_text().splitlines())
        self.writer._rotate_note("verdict", path)
        self.assertEqual(len(path.read_text().splitlines()), first,
                         "a repeat rotation with no new records must never move the file")

    def test_rotation_needs_real_entries(self) -> None:
        path = self.store.notes_root / "plain.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(f"line {i}" for i in range(700)) + "\n", encoding="utf-8")
        before = path.read_text()
        self.writer._rotate_note("plain", path)
        self.assertEqual(path.read_text(), before, "a file with no record markup never cuts")



class TestTranscriptWindow(PairTestCase):
    """The transcript sliding window: tail reading and rotation (item 4).

    What it fixes, measured, not guessed. The "46 thousand tokens" figure stood in
    the plan in three places, turning out the FILE size, while the tail read
    by default takes 40 records (~2k). Rotation solves another thing than "dropping
    46k": the tail read never parses the whole file, and `pairctl log` without
    `--limit` no longer dumps the whole journal into the terminal.
    """

    def setUp(self) -> None:
        super().setUp()
        import server
        self.server = server

    def fill(self, count: int, **fields: object) -> None:
        for index in range(count):
            self.store.append_transcript("test", "writer", i=index, **fields)

    def indexes(self, entries: list) -> list:
        return [e["i"] for e in entries if "i" in e]

    def ctl(self, *argv: str) -> int:
        return main(["--state", str(self.root), *argv])

    # ---------------------------------------------- tail reading

    def test_the_tail_equals_the_last_entries(self) -> None:
        """A tail is a tail, not "something tail-like".

        Order checks too: reading from the file end must return records in the same
        order as a straight read, or the journal reads backwards, and no before-after
        ever reads from it.
        """
        self.fill(500)
        whole = self.store.read_transcript(0)
        for limit in (1, 7, 20, 40, 499, 500):
            with self.subTest(limit=limit):
                self.assertEqual(self.indexes(whole[-limit:]),
                                 self.indexes(self.store.read_transcript(limit)),
                                 "the tail must match the full-read end")

    def test_a_limit_beyond_the_file_returns_everything(self) -> None:
        """Asked for more than exists — all serves, never blank or error."""
        self.fill(10)
        self.assertEqual(10, len(self.store.read_transcript(999)))

    def test_a_line_longer_than_the_read_block_is_not_torn(self) -> None:
        """A record longer than the read portion must arrive whole.

        Byte slicing cuts both the newline and the line in half. Gluing the partial
        head is the only rescue here, while a lost record never restores in an audit
        journal.
        """
        big = "z" * 400_000
        with self.store.transcript_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"kind": "small", "i": 1}, ensure_ascii=False) + "\n")
            handle.write(json.dumps({"kind": "huge", "blob": big}, ensure_ascii=False) + "\n")
            handle.write(json.dumps({"kind": "last", "i": 2}, ensure_ascii=False) + "\n")
        got = self.store.read_transcript(3)
        self.assertEqual(["small", "huge", "last"], [e["kind"] for e in got])
        self.assertEqual(big, got[1]["blob"], "a long record must arrive whole")

    def test_a_damaged_line_is_marked_and_does_not_stop_the_read(self) -> None:
        """A mid-line cut is routine with a killed process.

        Silently skipping such a line is allowed, silently dropping is banned: the event
        reads as never was. So the record marks it, and reading goes on.
        """
        with self.store.transcript_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"kind": "first", "i": 1}, ensure_ascii=False) + "\n")
            handle.write("{not json at all\n")
            handle.write(json.dumps({"kind": "after", "i": 2}, ensure_ascii=False) + "\n")
        got = self.store.read_transcript(3)
        self.assertEqual(1, sum(1 for e in got if e.get("_damaged")),
                         "a broken line must show, never vanish")
        self.assertEqual(["first", "after"],
                         [e["kind"] for e in got if not e.get("_damaged")],
                         "records on both sides of the cut must read")

    def test_rotation_does_not_happen_on_a_small_file(self) -> None:
        """A plain round must never rotate on every second call."""
        self.fill(20)
        self.assertFalse((self.store.root / "transcript-archive").exists(),
                         "a small journal never rotates")

    def test_chain_links_every_entry_to_the_previous(self) -> None:
        """Chain: every line carries the previous prev_hash, the first — genesis.
        A hand edit or a lost chunk breaks the chain visibly at once, in pair_transcript
        as chain.bad_at, not at an audit a week later."""
        self.fill(5)
        entries = self.store.read_transcript(0)
        self.assertEqual(self.server.CHAIN_GENESIS, entries[0].get("prev_hash"),
                         "the first line refers to genesis")
        for prev, cur in zip(entries, entries[1:]):
            self.assertEqual(self.server._transcript_hash(prev), cur.get("prev_hash"),
                             "every line refers to the previous hash")
        result = self.writer.tool_pair_transcript(limit=0)
        self.assertTrue(result["chain"]["ok"], "the whole chain must verify")
        self.assertEqual(5, result["chain"]["checked"], "all five lines counted")

    def test_chain_breaks_loudly_on_tampering(self) -> None:
        """A mid-chain swap reddens: the break shows explicit, never a silent skip."""
        self.fill(5)
        lines = self.store.transcript_path.read_text(encoding="utf-8").splitlines()
        mid = json.loads(lines[2])
        mid["i"] = 9999
        lines[2] = json.dumps(mid, ensure_ascii=False)
        self.store.transcript_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        result = self.writer.tool_pair_transcript(limit=0)
        self.assertFalse(result["chain"]["ok"], "a swap must break the chain")
        self.assertEqual(3, result["chain"]["bad_at"], "the break shows on the next line")

    def test_chain_breaks_loudly_on_a_damaged_line(self) -> None:
        """A damage line is a visible break too, not a skip: or a cut on a killed process
        would read as an event that never was."""
        self.fill(2)
        with self.store.transcript_path.open("a", encoding="utf-8") as handle:
            handle.write("{not json at all\n")
        result = self.writer.tool_pair_transcript(limit=0)
        self.assertFalse(result["chain"]["ok"], "a broken line breaks the chain visibly")
        self.assertIn("damaged", result["chain"]["reason"])

    def test_rotation_carries_the_chain_across_the_marker(self) -> None:
        """Rotation is the verification horizon: the live file starts at a marker, the tail
        before it leaves for the archive, the live-tail chain verifies from the marker."""
        with mock.patch.object(self.server, "TRANSCRIPT_MAX_BYTES", 20_000):
            self.fill(400, payload="y" * 40)
        entries = self.store.read_transcript(0)
        markers = [e for e in entries if e.get("kind") == "transcript_rotated"]
        self.assertTrue(markers, "the rotation marker must stand")
        marker = markers[0]
        archive = self.store.root / "transcript-archive" / "1.jsonl"
        archived = [json.loads(line) for line in
                    archive.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(marker["last_hash"], self.server._transcript_hash(archived[-1]),
                         "the marker carries the carried-away tail hash, not genesis")
        self.assertNotEqual(marker["last_hash"], self.server.CHAIN_GENESIS,
                            "genesis in the marker means reading past a shift")
        result = self.writer.tool_pair_transcript(limit=0)
        self.assertTrue(result["chain"]["ok"], "the post-rotation chain must verify")

    def test_chain_slice_from_the_middle_verifies(self) -> None:
        """A limit=N slice is no break: the slice first line is the anchor, the chain inside
        whole. Before round 42 tool_pair_transcript(limit=20) always returned chain
        broken, because the slice verified from genesis.
        """
        self.fill(10)
        result = self.writer.tool_pair_transcript(limit=4)
        self.assertEqual(4, result["count"], "the slice serves as tail")
        self.assertTrue(result["chain"]["ok"], "the chain inside the slice must verify")
        self.assertEqual(4, result["chain"]["checked"], "all four slice lines counted")

    def test_chain_slice_tampering_is_visible(self) -> None:
        """A swap inside the slice breaks the chain: the anchor hash mismatches line two."""
        self.fill(10)
        lines = self.store.transcript_path.read_text(encoding="utf-8").splitlines()
        mid = json.loads(lines[8])
        mid["i"] = 9999
        lines[8] = json.dumps(mid, ensure_ascii=False)
        self.store.transcript_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        result = self.writer.tool_pair_transcript(limit=4)
        self.assertFalse(result["chain"]["ok"], "a swap inside the slice must break the chain")

    def test_chain_legacy_head_without_prev_hash_is_pre_chain(self) -> None:
        """Lines older than the chain with no prev_hash mean a pre-chain era, not a break."""
        self.store.transcript_path.write_text(
            json.dumps({"ts": "2020-01-01T00:00:00+00:00", "kind": "test", "role": "writer", "i": -1},
                       ensure_ascii=False) + "\n", encoding="utf-8")
        self.fill(3)
        result = self.writer.tool_pair_transcript(limit=0)
        self.assertTrue(result["chain"]["ok"], "a legacy head never breaks the chain")
        self.assertEqual(1, result["chain"]["pre_chain"], "one pre-chain line counted")
        self.assertEqual(3, result["chain"]["checked"], "three chain lines verified")


    # ---------------------------------------------------- rotation

    def test_rotation_moves_old_entries_out_and_keeps_them(self) -> None:
        """Old leaves the live file but never vanishes: live plus archives cover one
        continuous tail, the freshest records all in place.

        History never deletes silent: a lost `role_collision` record cost us a night.
        Rotation must move, never dispose.
        """
        with mock.patch.object(self.server, "TRANSCRIPT_MAX_BYTES", 20_000):
            self.fill(400, payload="y" * 40)
        active = self.store.read_transcript(0)
        self.assertLess(len(active), 400, "the live file must shrink")
        archive = sorted((self.store.root / "transcript-archive").glob("*.jsonl"))
        self.assertTrue(archive, "rotation must carry something somewhere")
        archived = [e for piece in archive
                    for e in (json.loads(line) for line in
                              piece.read_text(encoding="utf-8").splitlines() if line.strip())]
        live = [e["i"] for e in active if "i" in e]
        kept = [e["i"] for e in archived if "i" in e]
        self.assertEqual(list(range(400 - len(live) - len(kept), 400)),
                         sorted(live + kept),
                         "live plus archives — one continuous tail with no holes")

    def test_rotation_says_that_it_happened(self) -> None:
        """Rotation writes into the journal, never passes silent.

        Or the journal lands shorter one day for no visible reason, and "where did the
        events go" turns unanswerable.
        """
        with mock.patch.object(self.server, "TRANSCRIPT_MAX_BYTES", 20_000):
            self.fill(400, payload="y" * 40)
        kinds = [e.get("kind") for e in self.store.read_transcript(0)]
        self.assertIn("transcript_rotated", kinds)

    def test_a_failed_rotation_does_not_lose_the_entry(self) -> None:
        """An archive failure must never drop the journal write.

        Journal order matters, record integrity matters more. So a rotation failure
        survives, never turns into a lost event.
        """
        with mock.patch.object(self.server, "TRANSCRIPT_MAX_BYTES", 10):
            with mock.patch("os.replace", side_effect=OSError("disk full")):
                self.store.append_transcript("test", "writer", i=7)
        self.assertIn("i", self.store.read_transcript(1)[0],
                      "the write must survive a rotation failure")

    # --------------------------------------------------------- `pairctl log`

    def test_log_prints_a_tail_by_default(self) -> None:
        """A default limit instead of "everything".

        Measured: with no limit the command dumped the whole live journal into the
        terminal — ~50k tokens a round, including for a human who merely looked at what
        happens. Those needing the whole file ask for it explicit.
        """
        self.fill(200)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(0, self.ctl("log"))
        lines = [l for l in out.getvalue().splitlines() if l.strip()]
        self.assertEqual(self.server.LOG_DEFAULT_LIMIT, len(lines))

    def test_log_says_that_it_truncated(self) -> None:
        """Silently serving 50 lines for 200 reads as the full journal.

        A human reading the journal to understand what happened must know the view
        is partial.
        """
        self.fill(200)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            with contextlib.redirect_stdout(io.StringIO()):
                self.ctl("log")
        self.assertIn("200", err.getvalue())
        self.assertIn("--all", err.getvalue())

    def test_log_all_prints_everything_and_says_nothing_about_truncation(self) -> None:
        """`--all` means an explicit "show all", and it never complains."""
        self.fill(200)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(0, self.ctl("log", "--all"))
        lines = [l for l in out.getvalue().splitlines() if l.strip()]
        self.assertEqual(200, len(lines))
        self.assertEqual("", err.getvalue().strip())

    def test_log_does_not_warn_when_it_showed_everything(self) -> None:
        """A truncation warning with no truncation is noise."""
        self.fill(3)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            with contextlib.redirect_stdout(io.StringIO()):
                self.ctl("log")
        self.assertEqual("", err.getvalue().strip())
