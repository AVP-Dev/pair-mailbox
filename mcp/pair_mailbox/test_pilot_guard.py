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


if __name__ == "__main__":
    unittest.main(verbosity=2)
