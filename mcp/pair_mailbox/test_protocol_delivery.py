#!/usr/bin/env python3
"""Protocol tests: delivery itself — addressing a pane, rollback, the ring that wakes a peer."""

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


class TestMessaging(PairTestCase):
    def test_label_is_applied_by_the_server(self) -> None:
        self.writer.tool_pair_state()
        self.peer.tool_pair_state()

        self.writer.tool_pair_send(to="reviewer-agent", text="here is my read on the bug")
        messages = self.peer.tool_pair_read()
        self.assertEqual(messages["count"], 1)

        message = messages["messages"][0]
        self.assertEqual(message["from"], "writer-agent")
        self.assertEqual(message["label"], "Chat from writer-agent: ")
        self.assertIn("here is my read", message["text"])

    def test_read_marks_messages_read(self) -> None:
        """Round 101 batch contract: at-least-once, then the ack commits.

        The pre-batch pin (second read returns 0) measured the old
        mark-on-reply semantics whose reply the client could lose. Now the
        retry returns the SAME letters under the SAME token with the cursor
        unmoved, and only the token's ack marks the mail read.
        """
        self.writer.tool_pair_state()
        self.peer.tool_pair_state()
        self.writer.tool_pair_send(to="reviewer-agent", text="one")
        self.writer.tool_pair_send(to="reviewer-agent", text="two")

        first = self.peer.tool_pair_read()
        self.assertEqual(first["count"], 2)
        second = self.peer.tool_pair_read()
        self.assertEqual(second["count"], 2, "a retry replays, never skips")
        self.assertEqual(second["ack_token"], first["ack_token"])
        self.assertEqual([m["text"] for m in second["messages"]],
                         [m["text"] for m in first["messages"]])
        self.assertEqual(0, (self.store.read().get("seen") or {}).get("reviewer-agent", 0),
                         "the cursor stays where the batch was issued")
        self.peer.tool_pair_read(ack_token=first["ack_token"])
        third = self.peer.tool_pair_read()
        self.assertEqual(third["count"], 0, "the ack commits, the next read is fresh")

    def test_send_without_subject_still_leaves_trace_in_transcript(self) -> None:
        """A blind send event (seq 257–258) never repeats.

        Proven in code 2026-10-07: bodies lay whole in mailbox, while the transcript
        event carried only an empty subject — the text was lost from the journal.
        """
        self.peer.tool_pair_state()
        self.writer.tool_pair_send(to="reviewer-agent", text="round plan: fixing send")
        sends = [e for e in self.store.read_transcript(0) if e.get("kind") == "send"]
        self.assertEqual(1, len(sends))
        self.assertIn("round plan", sends[0].get("subject", ""))

    def test_cannot_send_to_yourself_or_nowhere(self) -> None:
        self.writer.tool_pair_state()
        self.assertRefused(self.writer.tool_pair_send, to="writer-agent", text="me", contains="own role")
        self.assertRefused(self.writer.tool_pair_send, to="", text="me", contains="required")
        self.assertRefused(self.writer.tool_pair_send, to="peer", text="  ", contains="required")
        # Round-47 lesson: 4 letters left for an opencode-agent role missing from the
        # roster. The server built a dead mailbox, reporting delivered:true. A silent loss
        # is the same failure class the protocol must prevent. The roster outlives letters
        # — refusal first now.
        self.assertRefused(
            self.writer.tool_pair_send, to="ghost-role", text="hello",
            contains="never registered")
        self.assertRefused(
            self.writer.tool_pair_deliver, to="ghost-role", text="hello",
            contains="never registered")
        mailboxes_after = sorted(p.name for p in self.store.mailbox_root.glob("*"))
        self.assertNotIn("ghost-role", mailboxes_after,
                         "a dead mailbox must never create")

    def test_writer_may_wait_too(self) -> None:
        """Both roles may wait. This used to be forced to zero for the writer, to

        stop two agents blocking each other, but that killed the channel: an agent that
        cannot wait falls asleep and never notices an incoming message, so the human
        starts relaying by hand. A bounded wait cannot deadlock, because it expires and
        hands the caller back to work.
        """
        self.writer.tool_pair_state()
        self.peer.tool_pair_state()
        self.writer.tool_pair_claim()

        started = time.monotonic()
        result = self.writer.tool_pair_read(wait_seconds=1)
        elapsed = time.monotonic() - started

        self.assertGreater(result["waited_seconds"], 0.0, "the writer must be able to wait")
        self.assertLess(elapsed, 4.0, "but the wait stays bounded")

    def test_wait_is_capped(self) -> None:
        """The clamp is a function of the client budget, not a module constant.

        Round 101 moved the cap to `read_wait.effective_read_wait`: any wait
        above the protocol ceiling or the role's MCP budget comes back cut, and
        the answer says so with the named numbers instead of a silent clamp.
        """
        import read_wait

        # 45s sits inside both the 60s ceiling and the 65s profile budget: untouched.
        self.assertEqual((45.0, False), read_wait.effective_read_wait(45, 65_000))
        # Above the 60s protocol ceiling: cut to the ceiling, not reported as client-capped.
        self.assertEqual((60.0, False), read_wait.effective_read_wait(9999, 65_000))
        # A tight client budget cuts the wait and says by how much and why.
        wait, capped = read_wait.effective_read_wait(45, 20_000)
        self.assertEqual(15.0, wait, "20s budget minus the 5s margin")
        self.assertTrue(capped)

    def test_peer_wait_is_bounded(self) -> None:
        self.peer.tool_pair_state()
        started = time.monotonic()
        result = self.peer.tool_pair_read(wait_seconds=2)
        elapsed = time.monotonic() - started
        self.assertEqual(result["count"], 0)
        self.assertLess(elapsed, 5.0)

    def test_negative_wait_is_refused(self) -> None:
        self.assertRefused(self.peer.tool_pair_read, wait_seconds=-1, contains="negative")




class TestSessionAddressing(PairTestCase):
    """Pane addressing by session name instead of the server-wide `%N` number.

    The fix reason measured: the `%N` number runs server-wide inside one tmux server, it
    renumbers on pane recreation and rots in the process env, while
    `<session>:<window>.<pane>` survives both.

    The move price measured too, and it hides: tmux answers a MISSING pane with code
    0 and not an error but the NEIGHBOUR LIVE pane of the same window. `%N` never
    does that. So a session address without a query check reads worse, not better: a
    rotten address silently points at a foreign pane, and instead of a refusal the
    message leaves for the neighbour.
    """

    def setUp(self) -> None:
        super().setUp()
        import server
        self.server = server

    def live_pane(self) -> str:
        """The pane of this suite, returned into the env.

        The base setUp drops `TMUX_PANE` so suite roles never spam the operator
        window. Unfit here: addressing checks against a live pane, and the agent
        learns its pane exactly from this variable. Without returning it the tests
        would pass checking nothing.

        The pane rises inside `self.root` — the suite project root, not the operator
        pane. `_register` matches the pane cwd against the project root and refuses
        writing a foreign one, exactly the defect being fixed: showing the operator
        pane would check the wrong branch. Our own pane serves the rest of this class
        checks too — they address % as well.
        """
        pane = self._own_pane()
        if not pane:
            self.skipTest("the suite runs outside tmux")
        # `previous` reads BEFORE assigning, and that is no typo: a capture past the write
        # gives `previous == pane`, so restoring returns the suite own pane instead of the
        # prior value. Further down the next test `PairTestBase.setUp` reads `TMUX_PANE`
        # and remembers it as the "operator pane" — already killed, and the suite fell on
        # `pane_exists` depending on test order.
        # `pane_exists` depending on test order.
        previous = os.environ.get("TMUX_PANE")
        os.environ["TMUX_PANE"] = pane
        self.addCleanup(
            lambda: os.environ.__setitem__("TMUX_PANE", previous)
            if previous is not None else os.environ.pop("TMUX_PANE", None))
        return pane

    def _own_pane(self) -> str:
        """A live pane with cwd inside the suite project root, once per test.

        Rises once and reuses: a `split-window` per call would leave a dozen windows
        past the test, while the suite pane must stay one and the same.
        """
        cached = getattr(self, "_own_pane_cache", None)
        if cached is not None:
            return cached or ""
        # The pane rises in the PROJECT root, not the state dir: the `project_root` of
        # `Store` is the `.pair` parent, and a pane launched from `.pair` would stand
        # foreign to its own project.
        project = self.store.project_root
        project.mkdir(parents=True, exist_ok=True)
        pane = ""
        if shutil_which("tmux"):
            made = subprocess.run(
                ["tmux", "split-window", "-d", "-P", "-F", "#{pane_id}",
                 "-c", str(project)],
                capture_output=True, text=True, timeout=10)
            if made.returncode == 0 and made.stdout.strip():
                pane = made.stdout.strip()
                self.addCleanup(subprocess.run,
                                ["tmux", "kill-pane", "-t", pane],
                                capture_output=True, timeout=10)
        self._own_pane_cache = pane
        return pane

    def test_a_session_address_is_recognised_as_a_pane(self) -> None:
        """Address syntax differs from the number, and both forms must pass.

        The right side is about the server, not composer: `server.pane_exists` and
        `composer.pane_alive` must understand the same reference set, or the roster
        deletes a live-neighbour record as a phantom.
        """
        import composer
        self.assertEqual("address", composer.pane_ref_format("mail:0.1"))
        self.assertEqual("id", composer.pane_ref_format("%1"))
        self.assertEqual("", composer.pane_ref_format("opencode"),
                         "a process name is no pane")
        self.assertEqual("", composer.pane_ref_format("mail"), "a session with no window and pane")
        self.assertEqual("", composer.pane_ref_format("mail:x.y"), "window and pane are numbers")
        self.assertEqual("", composer.pane_ref_format("a:b:0.0"), "a session name with no colon")

    def test_a_live_session_address_is_alive(self) -> None:
        """A regression on a bug the fix would bring in the same step.

        `pane_exists` checked the line shape with a `%[0-9]+` regex, so `mail:0.0` — a
        live pane — answered False. Past that it means exactly one thing:
        `roster --prune` would delete a role sitting right now.
        """
        import server
        pane = self.live_pane()
        if not shutil_which("tmux"):
            self.skipTest("tmux not installed")
        from composer import session_address
        resolved = session_address(shutil_which("tmux"), pane)
        self.assertTrue(resolved, f"a live pane address {pane} must resolve")
        self.assertTrue(server.pane_exists(resolved),
                        f"a live pane at {resolved} — alive, not a phantom")
        self.assertTrue(server.pane_exists(pane), "the number of the same pane stays alive too")

    def test_a_stale_session_address_is_not_the_neighbour_pane(self) -> None:
        """The move main risk, checked on live tmux, not a mock.

        A two-pane session; `.1` dies. The `<session>:0.1` address points at a pane
        that is gone, and tmux silently answers about `.0` — the neighbour live one.
        Without matching the address against the query the delivery leaves for a
        foreign composer, with no refusal at all.
        """
        import composer
        tmux = shutil_which("tmux")
        if not tmux:
            self.skipTest("tmux not installed")
        session = f"pairtest-{os.getpid()}-{os.urandom(4).hex()}"
        self.addCleanup(subprocess.run, [tmux, "kill-session", "-t", session],
                        capture_output=True, timeout=10)
        made = subprocess.run([tmux, "new-session", "-d", "-s", session],
                              capture_output=True, text=True, timeout=10)
        if made.returncode != 0:
            self.skipTest(f"tmux refuses to create a session: {made.stderr.strip()[:80]}")
        subprocess.run([tmux, "split-window", "-t", session],
                       capture_output=True, timeout=10)
        stale = f"{session}:0.1"
        survivor = f"{session}:0.0"

        # Before death the address is real: both checks must confirm it.
        self.assertTrue(composer.pane_identity(tmux, stale)["exists"])
        self.assertTrue(composer.pane_identity(tmux, survivor)["exists"])

        subprocess.run([tmux, "kill-pane", "-t", stale], capture_output=True, timeout=10)

        ghost = composer.pane_identity(tmux, stale)
        self.assertFalse(ghost["exists"],
                         "a rotten session address is no pane, even when tmux "
                         "answers about the neighbour live one")
        self.assertFalse(ghost["alive"])
        self.assertEqual("", ghost["path"],
                         "a neighbour-pane cwd never passes for a rotten one: or "
                         "pane_is_foreign would own a foreign pane as ours")
        self.assertFalse(composer.pane_alive(tmux, stale))
        self.assertFalse(self.server.pane_exists(stale))
        # The neighbour stays alive here, and must remain distinct from the phantom.
        self.assertTrue(composer.pane_identity(tmux, survivor)["exists"])
        self.assertTrue(self.server.pane_exists(survivor))

    def test_the_same_pane_is_recognised_across_both_formats(self) -> None:
        """Our own pane arrives from the env as `%N`, lying in state as an address.

        Line equality diverges on that, and checks built on it silently stop working:
        `_ring` starts ringing our own pane, while the roster counts two panes where
        one stands. Matching must run through tmux — only it knows both are one pane.
        """
        import server
        pane = self.live_pane()
        if not shutil_which("tmux"):
            self.skipTest("tmux not installed")
        from composer import session_address
        address = session_address(shutil_which("tmux"), pane)
        if not address:
            self.skipTest("failed to get the session address")
        self.assertTrue(server.same_pane(pane, address),
                        f"{pane} and {address} — one pane")
        self.assertTrue(server.same_pane(address, pane), "order never matters")
        self.assertFalse(server.same_pane(address, "%999999"),
                         "a phantom never matches a live pane")
        self.assertFalse(server.same_pane("", address), "an empty reference is no pane")
        self.assertFalse(server.same_pane(address, None))

    def test_a_registered_role_is_recorded_by_session_address(self) -> None:
        """What exactly changes in state: `%N` writes no more.

        The address comes from the env (`TMUX_PANE`), because that is how the agent
        knows its pane; state takes the stable form. A live-tmux check: with a mock
        any implementation would pass, while exactly here the format drift had to be
        fixed.
        """
        pane = self.live_pane()
        session = PairSession("agy", self.store)
        session.tool_pair_state()
        recorded = self.store.read()["roles"]["agy"].get("pane")
        self.assertTrue(recorded, "pane recorded")
        self.assertNotEqual(pane, recorded,
                            f"expected a session address, but a number stands: {pane}")
        self.assertTrue(self.server.pane_exists(recorded),
                        "what stands recorded must be a live pane — or "
                        "the very next _register wipes it as rotten")

    def test_the_old_number_format_still_works_after_the_switch(self) -> None:
        """Backwards compatibility checks on live records, not words.

        State holds `%N` from prior runs. If reading stopped accepting them, the
        upgrade would break delivery for all live roles at once in one step — the
        very upgrade fixing the rot would cause the non-delivery.
        """
        pane = self.live_pane()
        self.store.mutate(lambda s: s.setdefault("roles", {}).setdefault("agy", {}).update(
            {"pane": pane, "pid": 999999}))
        newcomer = PairSession("claude", self.store)
        newcomer.tool_pair_state()
        self.assertEqual(pane, self.store.read()["roles"]["agy"].get("pane"),
                         "an old %N-format record must stay accepted")

    def test_a_failed_address_lookup_is_visible_in_the_transcript(self) -> None:
        """A silent fallback to `%N` decides for the human implicitly.

        If the session name never resolves, addressing stays unstable, and the human
        must see it. Or the pane-rot reason stays invisible forever, with not a word
        about it in the transcript.
        """
        self.live_pane()
        import composer
        # The stub hits the address record, not `pane_identity`: the same call serves the
        # "pane ours" check, and an empty answer from there would declare the pane
        # missing. An addressing refusal checks, not a pane refusal.
        real_write = composer.session_address
        identity_calls = []

        def fake(tmux_arg, pane_arg):
            if pane_arg == self._own_pane_cache:
                return ""                      # the address never resolved
            identity_calls.append(pane_arg)
            return real_write(tmux_arg, pane_arg)

        composer.session_address = fake
        self.addCleanup(setattr, composer, "session_address", real_write)
        session = PairSession("agy", self.store)
        session.tool_pair_state()
        pane = self.store.read()["roles"]["agy"].get("pane")
        self.assertTrue(pane, "the record never drops: it must work on %N too")
        self.assertEqual(pane, self._own_pane_cache,
                         "with no session address %N stays")
        kinds = [e["kind"] for e in self.store.read_transcript()]
        self.assertIn("pane_address_unavailable", kinds)

    def test_a_successful_address_switch_is_recorded_once(self) -> None:
        """A transcript write must never repeat on every call.

        `_register` runs before every tool, and on literal comparing the record would
        fall into the transcript dozens of times a session — exactly the growth ceiling
        this same task fixes.
        """
        self.live_pane()
        session = PairSession("agy", self.store)
        session.tool_pair_state()
        recorded = self.store.read()["roles"]["agy"].get("pane")
        self.assertTrue(recorded)
        for _ in range(3):
            session.tool_pair_state()
        switches = [e for e in self.store.read_transcript()
                    if e["kind"] == "pane_address_canonical"]
        self.assertEqual(1, len(switches),
                         "an address-format change is one event, not every call")




class TestDeliveryRollback(PairTestCase):
    """Delivery rollback must never destroy on an unverified screen.

    Found live: pair_deliver returned delivered=false with "rolled back", while the
    message lay in the neighbour composer. Cause — one unconfirmed frame.
    """

    def lagging_tui(self, body: str, lag_after: int = 1):
        """A TUI that redraws late: lag_after frames past input .capture serve a stale screen,
       only then the real one."""
        import composer

        state = {"typed": False, "frames": 0, "erased": 0}

        def fake_capture(tmux, pane):
            state["frames"] += 1
            if not state["typed"]:
                return ["$ ", ">"]
            if state["frames"] <= lag_after:
                return ["$ ", ">"]           # the screen not yet redrawn
            return ["$ ", "> " + body]

        def fake_type(tmux, pane, text):
            state["typed"] = True

        def fake_erase(tmux, pane, count):
            state["erased"] += count

        return composer, state, fake_capture, fake_type, fake_erase

    def deliver_to_lagging_pane(self, body: str, lag_after: int) -> tuple[dict, dict]:
        import composer

        composer_mod, state, fake_capture, fake_type, fake_erase = self.lagging_tui(body, lag_after)
        saved = (composer.capture, composer.type_text, composer.erase,
                 composer.pane_alive, composer.composer_line, composer.is_composer_empty,
                 composer._tmux)
        composer.capture = fake_capture
        composer.type_text = fake_type
        composer.erase = fake_erase
        composer.pane_alive = lambda tmux, pane: True
        composer.composer_line = lambda lines: lines[-1] if lines else None
        composer.is_composer_empty = lambda line: line.strip() in ("$ ", ">")
        composer._tmux = lambda: "/usr/bin/tmux"
        try:
            return composer.deliver("%7", body, submit=False), state
        finally:
            (composer.capture, composer.type_text, composer.erase,
             composer.pane_alive, composer.composer_line, composer.is_composer_empty,
             composer._tmux) = saved

    def test_a_stale_frame_is_retried_before_believed(self) -> None:
        """One stale frame never proves the insert missed.

        Measured live on a 30x70 pane: a lone BSpace shows in capture after ~2s.
        Checking 0.35s past typing ~1900 chars therefore reads a blank screen as "text
        never arrived" — and rolls back what arrived.
        """
        body = "a retry check"
        result, state = self.deliver_to_lagging_pane(body, lag_after=2)
        self.assertTrue(
            result.get("delivered"),
            f"past the redraw the delivery must count delivered, never rolled back: {result}",
        )
        self.assertEqual(0, state["erased"], "a delivered delivery never rolls back")

    def test_an_unverifiable_pane_is_not_rolled_back_blindly(self) -> None:
        """Rolling back on an unread screen wipes the composer blind.

        `erase(len(body))` never knows whose input that is. The neighbour or the human
        typed in this same pane — and BSpaces eat their text while the protocol reports
        "rolled back". An unverifiable delivery must say unverifiable, never destroy a
        foreign pane content.
        """
        body = "a neighbour message too long for one screen"
        result, state = self.deliver_to_lagging_pane(body, lag_after=99)
        self.assertLess(
            state["erased"], len(body),
            f"rolling back on an unread frame erased {state['erased']} chars "
            f"in a pane that may hold foreign input: {result}",
        )




class TestRingAndUnblock(PairTestCase):
    def test_permission_prompt_detection(self) -> None:
        from composer import detect_permission_prompt

        prompts = [
            ["Some output", "Allow execution of 'pytest'? [y/N]"],
            ["Running build", "Do you want to proceed? [y/n]"],
            ["Do you allow this tool call? [Y/n]"],
            ["Tool approval required", "Approve execution? [y/n/always]"],
        ]
        for lines in prompts:
            matched = detect_permission_prompt(lines)
            self.assertTrue(bool(matched), f"Failed to detect in {lines}")
            self.assertIn(matched, lines)

        not_prompts = [
            ["Press Enter to continue"],
            ["Allow me to read it?"],
            ["Do you have permission?"],
            ["def foo():", "    return x"],
            ["cost = prices[y] / n"],
            ["echo done"],
            [],
        ]
        for lines in not_prompts:
            self.assertIsNone(detect_permission_prompt(lines), f"False positive in {lines}")

    def test_composer_empty_uses_last_marker(self) -> None:
        from composer import is_composer_empty

        # Single prompt with no input
        self.assertTrue(is_composer_empty("> "))
        # Output before prompt that contains marker characters
        self.assertTrue(is_composer_empty("Cost: $0\n> "))
        # User input after prompt
        self.assertFalse(is_composer_empty("> echo $foo"))
        self.assertFalse(is_composer_empty("Cost: $0\n> pytest -v"))

    def test_ring_disabled_by_env(self) -> None:
        os.environ["PAIR_DISABLE_RING"] = "1"
        res = self.writer._ring("reviewer-agent", "test", "details")
        self.assertFalse(res["rung"])
        self.assertIn("PAIR_DISABLE_RING", res["why"])

    def test_ring_survives_an_invisible_composer(self) -> None:
        """`composer_line` returns None when no composer shows.

        `is_composer_empty(None)` falls on NoneType.rstrip, and before this fix any
        ring into a panel where the TUI redrew dropped the caller — meaning pair_note
        could fall whole over a foreign pane."""
        import composer
        self.store.mutate(lambda s: s.setdefault("roles", {})
                          .setdefault("reviewer-agent", {}).update({"pane": "%45"}))
        os.environ.pop("PAIR_DISABLE_RING", None)
        import server as server_mod
        orig_line = composer.composer_line
        orig_foreign = composer.pane_is_foreign
        orig_run = server_mod.subprocess.run
        composer.composer_line = lambda lines: None
        # Pane %45 never exists, and the identity check would reject it before reaching
        # the composer. The test is about an invisible composer, so pane belonging
        # declares upfront, never stubbed with an empty result: or it would check another
        # branch already.
        self.allow_panes()
        # EXECUTION and the path stub, not "real tmux". It used to stand
        # `composer.shutil.which = lambda: "/usr/bin/tmux"`, but `composer.shutil` is the
        # shutil module itself for the whole process, and the stub also hit
        # server.py:2075, where tmux resolves for the real subprocess.run.
        # The test is about composer VISIBILITY, not where tmux lives: on macOS Homebrew
        # puts it in /opt/homebrew/bin, and the test fell with FileNotFoundError, staying
        # green only on a machine with tmux in /usr/bin.
        #
        # `which` serves a path that surely exists (/usr/bin/false): or the test would
        # still demand tmux in PATH, answering "tmux missing from PATH" on a machine
        # without it — checking not the composer.
        orig_which = server_mod.shutil.which
        server_mod.shutil.which = lambda name: "/usr/bin/false"
        def fake_run(cmd, *args, **kwargs):
            # Pane %45 never exists: answering "alive" lets _ring reach the composer — its
            # visibility is the test subject.
            return subprocess.CompletedProcess(cmd, 0, stdout="0\n", stderr="")
        server_mod.subprocess.run = fake_run
        try:
            result = self.writer._ring("reviewer-agent", "note", "details")
        finally:
            composer.composer_line = orig_line
            composer.pane_is_foreign = orig_foreign
            server_mod.subprocess.run = orig_run
            server_mod.shutil.which = orig_which
        self.assertFalse(result["rung"], "never climb into an invisible composer")
        self.assertIn("invisible", result["why"])

    def test_ring_avoids_self_pane(self) -> None:
        os.environ.pop("PAIR_DISABLE_RING", None)
        os.environ["TMUX_PANE"] = "%99"
        self.store.mutate(lambda s: s.setdefault("roles", {})
                          .setdefault("reviewer-agent", {})
                          .update({"pane": "%99"}))
        res = self.writer._ring("reviewer-agent", "test", "details")
        self.assertFalse(res["rung"])
        self.assertIn("own pane", res["why"])

    def test_pair_unblock_validation(self) -> None:
        self.writer.tool_pair_state()
        self.peer.tool_pair_state()

        # Cannot unblock self
        self.assertRefused(lambda: self.writer.tool_pair_unblock(peer="writer-agent"))

        # Peer without tmux pane returns graceful status
        res = self.writer.tool_pair_unblock(peer="reviewer-agent")
        self.assertFalse(res["unblocked"])
        self.assertIn("has no tmux pane", res["why"])
        self.assertEqual(res["prompt"], "")

    def test_ring_probe_oserror_is_reported_not_raised(self) -> None:
        os.environ.pop("PAIR_DISABLE_RING", None)
        self.addCleanup(os.environ.update, PAIR_DISABLE_RING="1")
        self.allow_panes()
        self.store.mutate(lambda s: s.setdefault("roles", {})
                          .setdefault("reviewer-agent", {}).update({"pane": "%45", "pid": 999999}))
        import server as server_mod
        orig_run = server_mod.subprocess.run
        def boom(cmd, *args, **kwargs):
            raise OSError("tmux vanished between which() and launch")
        server_mod.subprocess.run = boom
        try:
            result = self.writer._ring("reviewer-agent", "test")
        finally:
            server_mod.subprocess.run = orig_run
        self.assertFalse(result["rung"])
        self.assertIn("never verifies", result["why"])
        self.assertIn("tmux", result["why"])
