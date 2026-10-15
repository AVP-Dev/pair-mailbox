#!/usr/bin/env python3
"""Event waking with no composer (wake-without-composer).

Checks the contract: a letter/note always leaves the wake/<role>.json file (the
pairpilot-poll guarantee), while _ring is only a wait-free optimization: a
busy/invisible composer returns at once, the tool never hangs.
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pair_test_base import PairTestCase  # noqa: E402


class TestEventWake(PairTestCase):
    def wake_file(self, role: str) -> Path:
        return self.root / "wake" / f"{role}.json"

    def test_send_writes_wake_even_when_ring_disabled(self) -> None:
        """A letter leaves the wake file even with ringing off.

        Or "the file channel as guarantee" reads words: at PAIR_DISABLE_RING=1
        (as in suites) the neighbour would learn nothing.
        """
        os.environ["PAIR_DISABLE_RING"] = "1"
        self.writer.tool_pair_state()
        self.peer.tool_pair_state()
        self.writer.tool_pair_send(to="reviewer-agent", text="take the task")
        path = self.wake_file("reviewer-agent")
        self.assertTrue(path.exists(), "send must write the wake file on the event")
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual("reviewer-agent", payload.get("role"))
        self.assertIn("message", payload.get("reasons", []))

    def test_note_writes_wake_for_peer(self) -> None:
        os.environ["PAIR_DISABLE_RING"] = "1"
        self.writer.tool_pair_state()
        self.peer.tool_pair_state()
        self.writer.tool_pair_note(kind="plan", text="the plan is ready")
        path = self.wake_file("reviewer-agent")
        self.assertTrue(path.exists(), "note must wake the neighbour by file")
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertTrue(any("note/plan" in str(r) for r in payload.get("reasons", [])))

    def test_note_does_not_ring_peers(self) -> None:
        """A note stays silent: the wake file yes, a blind composer ring no.

        tool_pair_note used to fan-ring every role on every note; at 30+ notes an
        evening that is N blind send-keys Enters into foreign composers. Waking
        runs over the wake file the pilot polls.
        """
        self.writer.tool_pair_state()
        self.peer.tool_pair_state()
        with mock.patch.object(self.writer, "_ring",
                               wraps=self.writer._ring) as ring:
            result = self.writer.tool_pair_note(kind="plan", text="the plan is ready")
        rung = [c for c in ring.call_args_list]
        self.assertEqual([], rung, "note must ring nobody")
        self.assertEqual({}, result.get("woke_peers"),
                         "woke_peers stays empty for compat")
        kinds = [e.get("kind") for e in self.store.read_transcript(limit=0)]
        self.assertNotIn("doorbell", kinds, "no rings in the transcript")
        path = self.wake_file("reviewer-agent")
        self.assertTrue(path.exists(), "the wake file still writes")

    def test_wake_merge_keeps_asked_at(self) -> None:
        """A pending wake appends, never overwrites.

        Or two reasons (unread + silence) would escalate twice — the same bug fixed
        in round 15 for the pilot.
        """
        from wake import record_wake
        os.environ["PAIR_DISABLE_RING"] = "1"
        self.writer.tool_pair_state()
        self.peer.tool_pair_state()
        state = self.store.read()
        record_wake(self.root, state, "reviewer-agent", "first", "text 1")
        first = json.loads(self.wake_file("reviewer-agent").read_text(encoding="utf-8"))
        record_wake(self.root, self.store.read(), "reviewer-agent", "second", "text 2")
        second = json.loads(self.wake_file("reviewer-agent").read_text(encoding="utf-8"))
        self.assertEqual(first.get("asked_at"), second.get("asked_at"),
                         "asked_at must persist, or patience resets")
        self.assertIn("first", second.get("reasons", []))
        self.assertIn("second", second.get("reasons", []))

    def test_ring_returns_immediately_when_composer_busy(self) -> None:
        """A busy composer means straight to file, with no 1.5s wait stealing input.

        Mutation: returning the deadline+sleep loop must redden the test (sleep
        gets called).
        """
        import composer
        import server as server_mod
        self.store.mutate(lambda s: s.setdefault("roles", {})
                          .setdefault("reviewer-agent", {}).update({"pane": "%45"}))
        os.environ.pop("PAIR_DISABLE_RING", None)
        orig_line = composer.composer_line
        orig_which = composer.shutil.which
        orig_foreign = composer.pane_is_foreign
        composer.composer_line = lambda lines: "> typing"
        composer.shutil.which = lambda name: "/usr/bin/tmux"
        composer.pane_is_foreign = lambda tmux, pane, root: (False, "")
        sleeps = []
        orig_sleep = server_mod.time.sleep
        server_mod.time.sleep = lambda s: sleeps.append(s)  # type: ignore[method-assign]
        orig_sub_run = server_mod.subprocess.run

        def fake_sub_run(cmd, *a, **kw):
            if isinstance(cmd, (list, tuple)) and len(cmd) >= 2 and cmd[1] == "display-message":
                return server_mod.subprocess.CompletedProcess(cmd, 0, stdout="0\n", stderr="")
            return orig_sub_run(cmd, *a, **kw)

        server_mod.subprocess.run = fake_sub_run  # type: ignore[method-assign]
        try:
            result = self.writer._ring("reviewer-agent", "message")
        finally:
            composer.composer_line = orig_line
            composer.shutil.which = orig_which
            composer.pane_is_foreign = orig_foreign
            server_mod.time.sleep = orig_sleep  # type: ignore[method-assign]
            server_mod.subprocess.run = orig_sub_run  # type: ignore[method-assign]
        self.assertFalse(result["rung"], "never climbing into a busy composer")
        self.assertIn("busy", result["why"])
        self.assertEqual([], sleeps, "composer waiting must stay single-shot with no sleep")

    def test_ring_failing_enter_records_miss_not_rung(self) -> None:
        """Human-observed hang: Enter send-keys fails, transcript must name
        the miss (doorbell_enter_unverified), never rung=True.

        Mutation: unconditional rung=True must redden this test.
        """
        import composer
        import server as server_mod
        self.store.mutate(lambda s: s.setdefault("roles", {})
                          .setdefault("reviewer-agent", {}).update({"pane": "%45"}))
        os.environ.pop("PAIR_DISABLE_RING", None)
        orig_line = composer.composer_line
        orig_which = composer.shutil.which
        orig_foreign = composer.pane_is_foreign
        composer.composer_line = lambda lines: ""
        composer.shutil.which = lambda name: "/usr/bin/tmux"
        composer.pane_is_foreign = lambda tmux, pane, root: (False, "")
        orig_sub_run = server_mod.subprocess.run
        calls = []

        def fake_sub_run(cmd, *a, **kw):
            calls.append(list(cmd) if isinstance(cmd, (list, tuple)) else cmd)
            if isinstance(cmd, (list, tuple)) and cmd[-1] == "Enter":
                return server_mod.subprocess.CompletedProcess(cmd, 1, stdout="", stderr="no such pane")
            if isinstance(cmd, (list, tuple)) and len(cmd) >= 2 and cmd[1] == "display-message":
                return server_mod.subprocess.CompletedProcess(cmd, 0, stdout="0\n", stderr="")
            return server_mod.subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        server_mod.subprocess.run = fake_sub_run  # type: ignore[method-assign]
        try:
            result = self.writer._ring("reviewer-agent", "message")
        finally:
            composer.composer_line = orig_line
            composer.shutil.which = orig_which
            composer.pane_is_foreign = orig_foreign
            server_mod.subprocess.run = orig_sub_run  # type: ignore[method-assign]
        self.assertFalse(result["rung"], "a failed Enter is not a ring")
        kinds = [json.loads(line).get("kind") for line in
                 (self.root / "transcript.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertIn("doorbell_enter_unverified", kinds)
        self.assertNotIn("doorbell", kinds)

    def test_ring_enter_accepted_and_submitted_rings(self) -> None:
        """C5 submit mode: rc=0 both sends and the prompt is gone from the
        composer after Enter — a real ring."""
        import composer
        import server as server_mod
        self.store.mutate(lambda s: s.setdefault("roles", {})
                          .setdefault("reviewer-agent", {}).update({"pane": "%45"}))
        os.environ.pop("PAIR_DISABLE_RING", None)
        orig_which = composer.shutil.which
        orig_foreign = composer.pane_is_foreign
        composer.shutil.which = lambda name: "/usr/bin/tmux"
        composer.pane_is_foreign = lambda tmux, pane, root: (False, "")
        states = iter(["> ", "> "])  # empty before, empty after
        orig_line = composer.composer_line
        orig_capture = composer.capture
        composer.composer_line = lambda lines: next(states)
        composer.capture = lambda tmux, pane: []
        orig_sub_run = server_mod.subprocess.run

        def fake_sub_run(cmd, *a, **kw):
            return server_mod.subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        server_mod.subprocess.run = fake_sub_run  # type: ignore[method-assign]
        try:
            result = self.writer._ring("reviewer-agent", "message")
        finally:
            composer.composer_line = orig_line
            composer.capture = orig_capture
            composer.shutil.which = orig_which
            composer.pane_is_foreign = orig_foreign
            server_mod.subprocess.run = orig_sub_run  # type: ignore[method-assign]
        self.assertTrue(result["rung"], "submitted prompt is a ring")
        kinds = [json.loads(line).get("kind") for line in
                 (self.root / "transcript.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertIn("doorbell", kinds)
        self.assertNotIn("doorbell_enter_unverified", kinds)

    def test_ring_retained_prompt_records_unverified(self) -> None:
        """C5 retained mode: rc=0 both sends but capture-pane still shows the
        injected prompt after Enter + retry — unverified, never rung."""
        import composer
        import server as server_mod
        self.store.mutate(lambda s: s.setdefault("roles", {})
                          .setdefault("reviewer-agent", {}).update({"pane": "%45"}))
        os.environ.pop("PAIR_DISABLE_RING", None)
        orig_which = composer.shutil.which
        orig_foreign = composer.pane_is_foreign
        composer.shutil.which = lambda name: "/usr/bin/tmux"
        composer.pane_is_foreign = lambda tmux, pane, root: (False, "")
        seen = {}
        orig_line = composer.composer_line
        orig_capture = composer.capture

        def fake_line(lines):
            # First (pre-send) call: empty. Post-Enter calls: prompt retained.
            if not seen:
                seen["pre"] = True
                return ""
            return "pair: message from writer-agent. Read pair_read."

        composer.composer_line = fake_line
        composer.capture = lambda tmux, pane: []
        orig_sub_run = server_mod.subprocess.run

        def fake_sub_run(cmd, *a, **kw):
            return server_mod.subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        server_mod.subprocess.run = fake_sub_run  # type: ignore[method-assign]
        try:
            result = self.writer._ring("reviewer-agent", "message")
        finally:
            composer.composer_line = orig_line
            composer.capture = orig_capture
            composer.shutil.which = orig_which
            composer.pane_is_foreign = orig_foreign
            server_mod.subprocess.run = orig_sub_run  # type: ignore[method-assign]
        self.assertFalse(result["rung"], "retained prompt is not a ring")
        self.assertIn("retained", result["why"])
        kinds = [json.loads(line).get("kind") for line in
                 (self.root / "transcript.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertIn("doorbell_enter_unverified", kinds)
        self.assertNotIn("doorbell", kinds)

    def test_ring_invisible_composer_records_refusal(self) -> None:
        """C2: an invisible composer refuses with a durable row, never silence."""
        import composer
        self.store.mutate(lambda s: s.setdefault("roles", {})
                          .setdefault("reviewer-agent", {}).update({"pane": "%45"}))
        os.environ.pop("PAIR_DISABLE_RING", None)
        orig_line = composer.composer_line
        orig_capture = composer.capture
        orig_which = composer.shutil.which
        orig_foreign = composer.pane_is_foreign
        composer.composer_line = lambda lines: None
        composer.capture = lambda tmux, pane: ["plain shell, no frame"]
        composer.shutil.which = lambda name: "/usr/bin/tmux"
        composer.pane_is_foreign = lambda tmux, pane, root: (False, "")
        try:
            result = self.writer._ring("reviewer-agent", "message")
        finally:
            composer.composer_line = orig_line
            composer.capture = orig_capture
            composer.shutil.which = orig_which
            composer.pane_is_foreign = orig_foreign
        self.assertFalse(result["rung"])
        self.assertIn("invisible", result["why"])
        rows = [json.loads(line) for line in
                (self.root / "transcript.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
        refused = [r for r in rows if r.get("kind") == "doorbell_refused"]
        self.assertTrue(refused, "every refusal leaves a durable row")
        self.assertEqual("%45", refused[-1].get("pane"))

    def test_ring_frame_with_chrome_labels_busy_not_invisible(self) -> None:
        """C3: a visible frame with nowhere to type is busy-with-chrome."""
        import composer
        self.store.mutate(lambda s: s.setdefault("roles", {})
                          .setdefault("reviewer-agent", {}).update({"pane": "%45"}))
        os.environ.pop("PAIR_DISABLE_RING", None)
        orig_line = composer.composer_line
        orig_capture = composer.capture
        orig_which = composer.shutil.which
        orig_foreign = composer.pane_is_foreign
        composer.composer_line = lambda lines: None
        composer.capture = lambda tmux, pane: ["  │  Build auto", "  ▌" + "█" * 10]
        composer.shutil.which = lambda name: "/usr/bin/tmux"
        composer.pane_is_foreign = lambda tmux, pane, root: (False, "")
        try:
            result = self.writer._ring("reviewer-agent", "message")
        finally:
            composer.composer_line = orig_line
            composer.capture = orig_capture
            composer.shutil.which = orig_which
            composer.pane_is_foreign = orig_foreign
        self.assertFalse(result["rung"], "hands off either way")
        self.assertIn("busy-with-chrome", result["why"])

if __name__ == "__main__":
    unittest.main(verbosity=2)
