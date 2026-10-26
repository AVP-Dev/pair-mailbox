"""The supervisor survives a failing rule — and the rest of the pass happens.

The measured death (2026-10-25, this project's own supervisor): one
`tmux send-keys` into a pane whose CLI had stopped consuming input hung for
the full 5-second timeout and raised `subprocess.TimeoutExpired`. The pilot's
`flush_outbox` caught `ComposerBusy` and nothing else, so the exception
escaped the tick, the whole `pairpilot` died, and EVERY rule it carried went
dark with it — the silence check, the stalled-agent check, and the monologue
watchdog. A dead supervisor is the one failure no watchdog can notice.

So the pass runs each rule inside its own guard: the failing rule is
recorded and skipped, and the rest of the pass still happens.

These are the supervisor's own tests, so they live beside the supervisor's
test file — one test file per source file — and the pilot's frozen test file
does not grow for them.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "mcp" / "pair_mailbox"))

_spec = importlib.util.spec_from_loader("pairpilot", None)
pairpilot = importlib.util.module_from_spec(_spec)
pairpilot.__dict__["__file__"] = str(ROOT / "bin" / "pairpilot")
exec(compile((ROOT / "bin" / "pairpilot").read_text(encoding="utf-8"), "pairpilot", "exec"),
     pairpilot.__dict__)


class PilotGuardCase(unittest.TestCase):
    """A supervisor over a throwaway room, with a fake tmux that answers nothing."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.tmp.name) / ".pair"
        self.state_dir.mkdir(parents=True)
        self._orig_tmux_pane = os.environ.pop("TMUX_PANE", None)
        self.fake_tmux = self._install_fake_tmux()
        self._orig_path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{self.fake_tmux.parent}{os.pathsep}{self._orig_path}"

    def _install_fake_tmux(self) -> Path:
        """A tmux that answers nothing and leaves a trace."""
        bindir = Path(self.tmp.name) / "bin"
        bindir.mkdir(parents=True, exist_ok=True)
        fake = bindir / "tmux"
        fake.write_text(
            "#!/bin/sh\n"
            "case \"$1\" in\n"
            "  capture-pane) printf '$ \\n' ;;\n"
            "esac\n"
            "exit 0\n",
            encoding="utf-8")
        fake.chmod(0o755)
        return fake

    def tearDown(self) -> None:
        if self._orig_tmux_pane is not None:
            os.environ["TMUX_PANE"] = self._orig_tmux_pane
        else:
            os.environ.pop("TMUX_PANE", None)
        os.environ["PATH"] = self._orig_path
        self.tmp.cleanup()

    def pilot(self) -> "pairpilot.Pilot":
        p = pairpilot.Pilot(self.state_dir, silence=600.0, interval=1)
        p.tmux = None
        orig = p._launch_commands
        here = self.state_dir

        def isolated(given=None):
            saved = pairpilot.ROOT
            try:
                pairpilot.ROOT = here
                return orig(given)
            finally:
                pairpilot.ROOT = saved

        p._launch_commands = isolated
        return p

    def write_state(self, **fields) -> dict:
        state = {
            "task": "test", "mode": "fix", "writer": None,
            "phase": "implement", "round": 1, "roles": {}, "seen": {},
            "patches": {}, "tasks": [],
        }
        state.update(fields)
        (self.state_dir / "state.json").write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        return state

    def transcript_kinds(self) -> list[str]:
        path = self.state_dir / "transcript.jsonl"
        if not path.exists():
            return []
        return [json.loads(line)["kind"]
                for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class TestOneRuleCannotKillTheSupervisor(PilotGuardCase):
    """A rule that throws must not take the other rules down with it."""

    def test_a_failing_rule_does_not_stop_the_pass(self) -> None:
        pilot = self.pilot()
        self.write_state()
        ran: list[str] = []

        def boom(state: dict) -> None:
            ran.append("boom")
            raise RuntimeError("tmux hung")

        pilot.flush_outbox = boom
        pilot.check_monologue = lambda state: ran.append("monologue")

        pilot.tick()

        self.assertIn("monologue", ran,
                      "the rule after the failing one must still run")
        self.assertIn("pilot_rule_failed", self.transcript_kinds(),
                      "the failing rule must be journalled, not merely absent")

    def test_a_failing_pass_does_not_raise(self) -> None:
        """Even the whole pass failing must not kill the supervisor."""
        pilot = self.pilot()
        self.write_state()

        def boom() -> dict:
            raise RuntimeError("the state read failed")

        pilot.state = boom

        result = pilot.tick()

        self.assertIsInstance(result, dict,
                              "a failed pass returns an empty state, not an exception")

    def test_the_startup_brief_still_reaches_the_log(self) -> None:
        """The guard must not swallow the one thing the human reads."""
        pilot = self.pilot()
        self.write_state(round=99, phase="implement")
        logged: list[str] = []
        original = pairpilot.log
        pairpilot.log = logged.append
        try:
            pilot.tick()
        finally:
            pairpilot.log = original
        self.assertTrue(any("round 99" in line for line in logged),
                        f"the brief must be logged; got {logged}")

    def test_a_damaged_wake_file_is_still_reported_once(self) -> None:
        """The guard must not turn a repeated report into a repeated WRITE."""
        pilot = self.pilot()
        self.write_state()
        damaged = pilot.wake_root / "agy.json"
        damaged.parent.mkdir(parents=True, exist_ok=True)
        damaged.write_text("{not json", encoding="utf-8")
        pilot.tick()
        before = len(self.transcript_kinds())
        pilot.tick()
        self.assertEqual(before, len(self.transcript_kinds()),
                         "the second tick writes nothing about the same file")


class TestPilotNotesAreAttributable(PilotGuardCase):
    """A note the pilot writes must be PROVABLE against the journal.

    The measured block (2026-10-26, this project's own round 146): the
    monologue cut wrote `notes/fix.md` by hand — a timestamp and a role, and
    nothing else. The notes-consistency gate reads the note's own sha and
    attempt and matches them against the journal's `note` rows; a note with
    no sha cannot be attributed, so the gate called it forged and BLOCKED the
    round close. The supervisor's own record of its work was the thing that
    stopped the round from shipping.

    So the pilot writes notes the way the server writes them: normalized body,
    sha, attempt counted from the journal, and a `note` row carrying both.
    """

    def test_the_note_carries_a_sha_and_an_attempt(self) -> None:
        pilot = self.pilot()
        self.write_state()
        pilot._note("fix", "a monologue was cut")
        text = (self.state_dir / "notes" / "fix.md").read_text(encoding="utf-8")
        self.assertIn("(note ", text, "the header must carry the sha and the attempt")
        self.assertRegex(text, r"\(note [0-9a-f]{12} attempt 1\)")

    def test_the_journal_row_proves_the_note(self) -> None:
        pilot = self.pilot()
        self.write_state()
        pilot._note("fix", "a monologue was cut")
        rows = [json.loads(line) for line
                in (self.state_dir / "transcript.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()]
        note_rows = [r for r in rows if r.get("kind") == "note"]
        self.assertTrue(note_rows, "the note must leave a journal row")
        self.assertEqual("fix", note_rows[-1]["note_kind"])
        self.assertTrue(note_rows[-1]["note_sha"])
        self.assertEqual(1, note_rows[-1]["note_attempt"])

    def test_the_same_body_twice_is_attributed_not_swallowed(self) -> None:
        pilot = self.pilot()
        self.write_state()
        pilot._note("fix", "the same body")
        pilot._note("fix", "the same body")
        rows = [json.loads(line) for line
                in (self.state_dir / "transcript.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()]
        note_rows = [r for r in rows if r.get("kind") == "note"]
        self.assertEqual([1, 2], [r["note_attempt"] for r in note_rows],
                         "the second identical body is attempt 2, not a silent duplicate")

    def test_the_note_passes_the_consistency_gate(self) -> None:
        """The gate is the point: a pilot note must not block the close."""
        sys.path.insert(0, str(ROOT / "mcp" / "pair_mailbox"))
        from pair_notes_consistency import check_notes
        pilot = self.pilot()
        self.write_state()
        pilot._note("fix", "a monologue was cut")
        ok, why = check_notes(self.state_dir)
        self.assertTrue(ok, why)


class TestBlockedComposerIsReported(PilotGuardCase):
    """A composer holding somebody ELSE's text must be REPORTED, not obeyed.

    The measured silence (2026-10-26, this project's own round 146): the
    critic's pane sat frozen for 2.8 hours with the human's unsent line in
    its composer. Every delivery was correctly refused (the text is not ours,
    so it stays untouched), every wake was raised, every escalation was
    queued... and the one person who could unblock it never learned that it
    was blocked.

    The refusal was right. The silence was the defect.

    The room here is FAKE, and it must stay fake. The first version of this
    test pointed at the live pane `%8` and asked the REAL tmux about it, so it
    passed only while that pane happened to hold text, and failed the moment
    the human's draft was submitted. A test whose verdict depends on what the
    operator is typing is not a test — the critic found exactly that.
    """

    def _room(self, pilot, composer_lines: list[str]) -> None:
        """A room of one pane, with a composer of the given content.

        `pane_alive` and `capture` are mocked in the module the rule imports
        them from — `guard` does a lazy `from composer import ...`, so the
        names are looked up at call time on the `composer` module itself.
        """
        import composer
        original_alive = composer.pane_alive
        original_capture = composer.capture
        original_run = composer.subprocess.run

        def fake_run(cmd, **kwargs):
            if "capture-pane" in cmd:
                import subprocess
                return subprocess.CompletedProcess(cmd, 0, "\n".join(composer_lines), "")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        composer.pane_alive = lambda tmux, pane: True
        composer.capture = lambda tmux, pane: list(composer_lines)
        composer.subprocess.run = fake_run
        self.addCleanup(setattr, composer, "pane_alive", original_alive)
        self.addCleanup(setattr, composer, "capture", original_capture)
        self.addCleanup(setattr, composer.subprocess, "run", original_run)

    def _run_rule(self, pilot) -> list[str]:
        """Run the rule; return what it said."""
        logged: list[str] = []
        import guard
        guard.blocked_composer_rule(pilot, pilot.state(), logged.append)
        return logged

    def test_a_busy_composer_is_reported_to_the_human(self) -> None:
        pilot = self.pilot()
        pilot.tmux = "/usr/bin/tmux"
        self.write_state(roles={"agy": {"pane": "%8", "pid": 1, "last_seen": "now"}})
        self._room(pilot, ["> the human's unsent line"])

        logged = self._run_rule(pilot)

        self.assertTrue(any("composer holds text that is not ours" in line
                            for line in logged),
                        f"the block must be reported; got {logged}")
        self.assertIn("pilot_composer_blocked", self.transcript_kinds())

    def test_a_free_composer_is_not_reported(self) -> None:
        pilot = self.pilot()
        pilot.tmux = "/usr/bin/tmux"
        self.write_state(roles={"agy": {"pane": "%8", "pid": 1, "last_seen": "now"}})
        self._room(pilot, ["> "])

        logged = self._run_rule(pilot)

        self.assertEqual([], logged)
        self.assertNotIn("pilot_composer_blocked", self.transcript_kinds())

    def test_no_tmux_is_not_a_block(self) -> None:
        """With no tmux there is no room to look at, and nothing to report."""
        pilot = self.pilot()
        pilot.tmux = None
        self.write_state(roles={"agy": {"pane": "%8", "pid": 1, "last_seen": "now"}})

        logged = self._run_rule(pilot)

        self.assertEqual([], logged)
        self.assertNotIn("pilot_composer_blocked", self.transcript_kinds())

    def test_the_report_never_types_into_the_composer(self) -> None:
        """The rule REPORTS. It never erases the human's text."""
        pilot = self.pilot()
        pilot.tmux = "/usr/bin/tmux"
        self.write_state(roles={"agy": {"pane": "%8", "pid": 1, "last_seen": "now"}})
        self._room(pilot, ["> the human's unsent line"])
        sent: list[list[str]] = []
        import composer
        original_run = composer.subprocess.run
        composer.subprocess.run = lambda cmd, **kw: sent.append(list(cmd))
        try:
            self._run_rule(pilot)
        finally:
            composer.subprocess.run = original_run
        self.assertFalse(any("send-keys" in " ".join(cmd) for cmd in sent),
                         "the rule must never type into or erase the composer")


if __name__ == "__main__":
    unittest.main(verbosity=2)
