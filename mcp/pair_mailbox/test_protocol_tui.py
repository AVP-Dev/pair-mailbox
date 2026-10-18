#!/usr/bin/env python3
"""Protocol tests: the composer's geometry — overflow, corner frames, status vs composer, TUI is not a bus."""

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


class TestOverflowedComposer(PairTestCase):
    """A collapsed long input means presence, not absence.

    Delivery checks look for the TAIL of the typed text. When the text outgrows the
    visible area, the TUI collapses it into "↑ N more lines", the tail leaves the screen,
    and the check calls unconfirmed delivery on text standing on screen. Past that
    the message duplicates into the mailbox — the human sees the same thing twice:
    in the mailbox and in the composer. Observed live on 4259 chars.
    """

    OVERFLOWED = ["", "  some output", "> ↑ 14 more lines"]
    PLAIN = ["", "  some output", ">"]

    def test_overflow_is_detected(self) -> None:
        import composer
        self.assertTrue(composer.composer_overflowed(self.OVERFLOWED))
        self.assertFalse(composer.composer_overflowed(self.PLAIN),
                         "an ordinary prompt never counts as overflow")

    def test_overflow_needs_an_actual_composer(self) -> None:
        """Overflow with no composer means no composer, not overflow."""
        import composer
        self.assertFalse(composer.composer_overflowed(["  frame only", "  ═══"]))

    def test_delivery_over_an_overflowed_composer_counts_as_delivered(self) -> None:
        """The main invariant: on-screen text never doubles into the mailbox."""
        import composer
        sent: list[str] = []

        def fake_run(cmd, **kw):
            if "send-keys" in cmd:
                sent.append(cmd[-1])
            return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        orig = (composer.subprocess.run, composer.capture, composer._tmux,
                composer.pane_alive)
        # pane_alive also walks tmux through subprocess.run, deciding "no pane" on empty
        # output. Stub it apart, or the test would check the stub behaviour, not delivery.
        # behaviour, not delivery.
        composer.subprocess.run = fake_run
        # The composer starts EMPTY, or deliver rightly refuses "busy". Overflow appears
        # after typing: exactly what happens live when typed text outgrows the line.
        # when typed text outgrows the line.
        frames = [self.PLAIN, self.OVERFLOWED]

        def fake_capture(tmux, pane):
            return frames.pop(0) if len(frames) > 1 else self.OVERFLOWED

        composer.capture = fake_capture
        composer._tmux = lambda: "tmux"
        composer.pane_alive = lambda tmux, pane: True
        try:
            res = composer.deliver("%1", "very long text " * 300, wait_for_empty=0)
        finally:
            (composer.subprocess.run, composer.capture, composer._tmux,
             composer.pane_alive) = orig
        self.assertTrue(res["delivered"], "an overflowed composer means delivery, not refusal")
        self.assertTrue(res.get("tail_hidden"))
        self.assertNotIn("unverified", res,
                         "confirmation never issues when the text provably stands on screen")
        self.assertIn("Enter", sent, "Enter still sends")

    def test_enter_that_does_not_submit_reports_retained(self) -> None:
        """Round 98: Enter sent, text still in the composer — retained, never submitted.

        The human saw the nudge hanging unsent (finding-human-nudge-enter-unverified):
        deliver sent Enter and claimed delivered=True. Post-Enter capture decides.
        """
        import composer
        sent: list[str] = []

        def fake_run(cmd, **kw):
            if "send-keys" in cmd:
                sent.append(cmd[-1])
            return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        body_frames = [["", "  some output", ">"],
                       ["", "  some output", "> nudge text here"],
                       ["", "  some output", "> nudge text here"],
                       ["", "  some output", "> nudge text here"]]

        def fake_capture(tmux, pane):
            return body_frames.pop(0) if len(body_frames) > 1 else body_frames[0]

        orig = (composer.subprocess.run, composer.capture, composer._tmux,
                composer.pane_alive)
        composer.subprocess.run = fake_run
        composer.capture = fake_capture
        composer._tmux = lambda: "tmux"
        composer.pane_alive = lambda tmux, pane: True
        try:
            res = composer.deliver("%1", "nudge text here", wait_for_empty=0)
        finally:
            (composer.subprocess.run, composer.capture, composer._tmux,
             composer.pane_alive) = orig
        self.assertEqual("retained", res.get("submit_state"),
                         "text still on screen after two Enters is retained")
        self.assertEqual(2, sent.count("Enter"), "retry sends Enter once more")
        self.assertIn("why", res)

    def test_a_composer_longer_than_the_screen_still_counts_as_delivered(self) -> None:
        """The third collapse mode, worse than the two before.

        `composer_overflowed` catches the TUI honestly writing "↑ 14 more lines". But
        opencode and Antigravity do other: longer text draws FROM THE START, everything
        below leaves past the bottom edge, with no marker at all. Measured in our own
        composer on a 1200-char text: the screen holds `MARKERSTART`, misses `MARKEREND`,
        misses `↑` too.

        Hence every long-message delivery reported unconfirmed: no tail stands on
        screen physically, seeking it is pointless. The neighbour got the message and
        ran it — only the report suffered, with the human thinking delivery broke.

        So the reconcile runs on the start: the only guaranteed visible one.
        """
        import composer
        body = "MSG-START " + ("word " * 200) + " MSG-END"
        # The whole screen: only the typed top shows, the tail nowhere at all.
        screen = [
            "  program output",
            "  ┃",
            f"  ┃  {body[:120]}",
            "  ┃  word word word word word word word word",
            "  ┃  word word word word word word word word",
            "  ┃",
            "  Build · opencode",
        ]
        self.assertFalse(composer.composer_overflowed(screen),
                         "this mode owns no marker, with a separate check for it")
        self.assertTrue(composer.tail_present(screen, body),
                        "typed start on screen means the text delivered")
        self.assertFalse(
            "MSG-END" in " ".join(" ".join(screen).split()),
            "the check must see no tail stands on screen at all — "
            "or the test swaps the cause for a symptom")

    def test_text_that_is_not_on_screen_at_all_is_not_delivered(self) -> None:
        """The reverse: a start check must never agree to emptiness.

        Seeking the start anywhere on screen lets another text with the same word count
        Seeking the start anywhere on screen lets another text with the same word count
        as delivery. So the reconcile runs on the composer, not the whole screen at
        once.
        """
        import composer
        body = "MSG-START and tail"
        self.assertFalse(composer.tail_present(["  output", "  ┃", "  Build"], body),
                         "an empty composer means no delivery")



class TestInputBoxCornerVsSideFrame(PairTestCase):
    """The input field differs from the side frame by corner geometry.

    The opencode input-field bottom edge starts at the `╹` corner, while pane side
    frames start at the `┃` vertical. Counting any line with a frame glyph as a field
    would ... [INFERENCE] placeholder — replaced below.
    """

    def test_lower_corner_is_the_input_row(self) -> None:
        from composer import composer_line
        panel = ["  ┃ Build · something", "  ╹▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀",
                 "   ⬝⬝⬝ esc interrupt  320.9K (31%) · $0.00  ctrl+p commands", ""]
        found = composer_line(panel)
        self.assertIsNotNone(found)
        self.assertTrue(found.strip().startswith("╹"), f"expected the bottom edge, got {found!r}")

    def test_the_input_row_beats_a_tool_call_echo(self) -> None:
        """`›` sits in PROMPT_MARKERS, but a tool-call echo is no composer."""
        from composer import composer_line, is_composer_empty
        panel = ["  Thought", "     › pair.pair_send [to=agy, subject=... done]",
                 "  ┃  Build · something", "  ╹▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀",
                 "   ⬝⬝⬝ esc interrupt  320.9K (31%) · $0.00  ctrl+p commands", ""]
        found = composer_line(panel)
        self.assertIsNotNone(found)
        self.assertTrue(found.strip().startswith("╹"),
                        f"a tool-call echo drowned the real composer: {found!r}")
        self.assertTrue(is_composer_empty(found),
                        "a free composer must never read busy over an echo")

    def test_a_panel_of_frames_alone_is_not_writable(self) -> None:
        from composer import composer_line
        panel = ["  ┃  Build · something", "  ┃", "  ┃", ""]
        self.assertIsNone(composer_line(panel),
                          'a frame-only pane takes typing nowhere: busy, not free')

    def test_path_row_below_the_border_does_not_hide_the_input(self) -> None:
        """A live opencode frame: a path line runs under the edge, not a status.

        Measured 2026-10-07: the '╹edge + ~/work/... + empty' tail broke before the
        edge, composer_line returned the '› pair_read' echo, the detector said
        "busy" — the pilot piled outbox, the critic saw no letters.
        """
        from composer import composer_line, is_composer_empty
        panel = ["     › pair.pair_read [limit=10, wait_seconds=10]",
                 "  ┃  Build · x", "  ┃", "  ╹" + "▀" * 30,
                 "  ~/work/umputum:round-34  37.9K  ctrl+p commands", ""]
        found = composer_line(panel)
        self.assertIsNotNone(found)
        self.assertTrue(found.strip().startswith("╹"),
                        f"a path line hid the input-field edge: {found!r}")
        self.assertTrue(is_composer_empty(found))



class TestStatusLineIsNotAComposer(PairTestCase):
    """`$0.00` in the TUI status line is a price, not a shell prompt.

    Observed live: the opencode status line reads `... umputum:master 412.4K (39%)
    · $0.00 ctrl+p commands`. The `$` there was declared a marker, `is_composer_empty`
    took `0.00  ctrl+p commands` past it, counted the composer busy, and delivery
    refused forever: a neighbour could never wake a neighbour, and `tmux send-keys`
    went by hand.
    """

    STATUS = "  ⬝⬝ esc interrupt  412.4K (39%) · $0.00  ctrl+p commands"

    def test_a_status_line_is_not_taken_as_the_composer(self) -> None:
        from composer import composer_line
        self.assertIsNone(composer_line(["some output", self.STATUS]),
                          "a status line must never turn into an input line")

    def test_a_shell_prompt_ending_in_dollar_is_still_a_prompt(self) -> None:
        """A regression my first fix version brought: a real prompt `user@host:~$` never
        starts with the sign, and I took it for a status line — meaning it would break
        delivery into a plain shell."""
        from composer import composer_line, is_composer_empty
        for prompt, expect_empty in ((["user@host:~$ "], True),
                                     (["user@host:~$ rm -rf"], False),
                                     (["root@box:/srv$ "], True)):
            with self.subTest(prompt=prompt[0]):
                line = composer_line(prompt)
                self.assertIsNotNone(line)
                self.assertEqual(expect_empty, is_composer_empty(line))

    def test_an_empty_shell_prompt_is_still_empty(self) -> None:
        from composer import composer_line, is_composer_empty
        self.assertTrue(is_composer_empty(composer_line(["$ "]) or ""))
        self.assertTrue(is_composer_empty(composer_line(["output", "$ "]) or ""))

    def test_a_busy_shell_prompt_is_still_busy(self) -> None:
        from composer import composer_line, is_composer_empty
        self.assertFalse(is_composer_empty(composer_line(["$ python3 test.py"]) or ""))
        self.assertFalse(is_composer_empty(composer_line(["> half typed"]) or ""))

    def test_the_cost_rule_does_not_depend_on_brand_words(self) -> None:
        """Brand-mark fixes never hold: the very first theme change breaks them. The only
        stable sign is the value past `$`."""
        from composer import looks_like_status_line
        self.assertTrue(looks_like_status_line("anything at all · $0.00 · ctrl+p commands"))
        self.assertFalse(looks_like_status_line("user@host:~$"))
        self.assertFalse(looks_like_status_line("> "))
        self.assertFalse(looks_like_status_line("a line with no dollar at all"))



class TestTuiIsNotABus(PairTestCase):
    """The human directive: the terminal is a display, not a bus between agents.

    "No text walls over send-keys". What checks is not missing certain words but
    the main property: how many chars leave for the neighbour composer. The pointer
    must stay short holding no letter text, even when the letter runs two screens
    long.
    """

    def setUp(self) -> None:
        super().setUp()
        import composer
        self.composer = composer
        self.printed: list[str] = []
        self.store.mutate(lambda state: state.setdefault("roles", {}).update(
            {"reviewer-agent": {"first_seen": "then", "last_seen": "then",
                                "pid": os.getpid(), "pane": "%55"}}))

    def _capture_pointer(self, text: str) -> str:
        """Run the delivery with printing stubbed, returning what left for the composer."""
        orig_deliver = self.composer.deliver
        orig_foreign = self.composer.pane_is_foreign
        self.allow_panes()

        def fake_deliver(pane, body, wait_for_empty=0.0, submit=True):
            self.printed.append(body)
            return {"delivered": True, "chars": len(body), "pane": pane,
                    "submitted": submit}

        self.composer.deliver = fake_deliver
        try:
            self.writer.tool_pair_deliver(to="reviewer-agent", text=text)
        finally:
            self.composer.deliver = orig_deliver
            self.composer.pane_is_foreign = orig_foreign
        return self.printed[0] if self.printed else ""

    def test_the_composer_gets_a_pointer_and_not_the_message(self) -> None:
        """The TUI takes a line, not a letter.

        Length checks, not missing single words: the letter text may hold any word,
        and a word-list ban bypasses trivially. The true demand: no message text lies
        in the composer.
        """
        payload = "VERY-LONG-LETTER-TEXT " + ("details " * 300)
        printed = self._capture_pointer(payload)
        self.assertTrue(printed, "the pointer must print, or the neighbour never wakes")
        self.assertNotIn("VERY-LONG", printed, "letter text must never reach the TUI")
        self.assertIn("pair_read", printed, "the pointer must say where to fetch the text")

    def test_the_pointer_is_short_whatever_the_message_length(self) -> None:
        """The pointer never grows with the letter.

        Or "pointer" silently turns a wall again — merely because the letter ran long.
        """
        short = self._capture_pointer("short")
        self.printed.clear()
        long_pointer = self._capture_pointer("long " * 500)
        self.assertLessEqual(len(long_pointer), len(short) + 5,
                             "the pointer must read the same length")

    def test_the_message_is_in_the_mailbox_even_if_the_pointer_never_lands(self) -> None:
        """The letter in the box never depends on whether the pointer arrived.

        Exactly the new-order meaning: no foreign TUI can lose the container. The old
        scheme lost the message when the composer stayed busy.
        """
        orig_deliver = self.composer.deliver
        orig_foreign = self.composer.pane_is_foreign
        self.allow_panes()
        self.composer.deliver = lambda *a, **kw: (_ for _ in ()).throw(
            self.composer.ComposerBusy("busy"))
        try:
            res = self.writer.tool_pair_deliver(to="reviewer-agent", text="never lose")
        finally:
            self.composer.deliver = orig_deliver
            self.composer.pane_is_foreign = orig_foreign
        read = self.peer.tool_pair_read(wait_seconds=0)
        self.assertEqual(read["count"], 1)
        self.assertIn("never lose", read["messages"][0]["text"])
        self.assertTrue(res["delivered"])



class TestComposerOfAnOpencodePane(PairTestCase):
    """OpenCode owns no ASCII prompt marker: the input field is a frame over status.

    The composer searched only as "a line with a prompt marker", so an OpenCode
    pane never recognized, `composer_line` returned None, and the caller must count
    None as busy. Delivery and ringing into OpenCode stood impossible in principle,
    not at times — silently landing in the box.
    """

    BORDER = "  \u2579\u2580\u2580\u2580\u2580\u2580\u2580\u2580\u2580  "
    STATUS = "  \u25a0\u25a0\u25a0 esc interrupt   92.0K (9%) \u00b7 $0.00  ctrl+p commands"

    def test_border_above_status_is_recognised_as_the_composer(self) -> None:
        from composer import composer_line
        found = composer_line(["", self.BORDER, self.STATUS])
        self.assertIsNotNone(found, "a frame over status is the OpenCode composer")
        self.assertEqual(found.strip(), self.BORDER.strip())

    def test_a_pristine_border_is_an_empty_composer(self) -> None:
        from composer import composer_line, is_composer_empty
        found = composer_line(["", self.BORDER, self.STATUS])
        self.assertTrue(is_composer_empty(found))

    def test_a_typed_border_is_a_busy_composer(self) -> None:
        """Text in the frame means a busy composer, never to interrupt."""
        from composer import composer_line, is_composer_empty
        typed = "  \u2579\u2580\u2580 typing right now \u2580\u2580  "
        found = composer_line(["", typed, self.STATUS])
        self.assertIsNotNone(found)
        self.assertFalse(is_composer_empty(found))

    def test_a_bare_frame_still_yields_none(self) -> None:
        """A frame-only pane types nowhere — "busy", not "free"."""
        from composer import composer_line
        self.assertIsNone(composer_line(["  \u2500\u2500\u2500\u2500\u2500\u2500  "]))

    def test_the_block_glyph_is_inside_the_frame_range(self) -> None:
        """\u2580 stood one above the old edge, and the frame missed fullmatch."""
        import re
        from composer import BOX_GLYPH
        self.assertTrue(re.fullmatch(rf"[{BOX_GLYPH}]+", "\u2580\u2580\u2580"))
