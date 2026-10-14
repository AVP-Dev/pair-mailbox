"""Tests for anti_mock_gate.py: blocking live mocks, passing test ones.

One test file per source: `anti_mock_gate.py` -> `test_anti_mock_gate.py`
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import anti_mock_gate
import session_facts


def facts_with(flags: list[dict]) -> dict:
    return {"version": session_facts.FACTS_VERSION, "head": "h",
            "dirty_hash": "d", "touched": [], "untracked": [],
            "diff_stat": "", "mock_flags": flags, "test_runs": []}


class TestJudge(unittest.TestCase):
    def test_clean_passes(self) -> None:
        verdict = anti_mock_gate.judge(facts_with([]))
        self.assertTrue(verdict["ok"])
        self.assertEqual(verdict["blockers"], [])

    def test_combat_mock_blocks(self) -> None:
        verdict = anti_mock_gate.judge(facts_with(
            [{"file": "web/app.js", "line": 3, "pattern": "mock-id",
              "match": "mock-abc", "reason": "r", "test_path": False}]))
        self.assertFalse(verdict["ok"])
        self.assertEqual(len(verdict["blockers"]), 1)

    def test_test_path_warns_only(self) -> None:
        verdict = anti_mock_gate.judge(facts_with(
            [{"file": "tests/x.py", "line": 1, "pattern": "mock-word",
              "match": "mock", "reason": "r", "test_path": True}]))
        self.assertTrue(verdict["ok"])
        self.assertEqual(len(verdict["warnings"]), 1)

    def test_local_state_flagged_for_critic(self) -> None:
        verdict = anti_mock_gate.judge(facts_with(
            [{"file": "web/app.js", "line": 7, "pattern": "local-state",
              "match": "useState(", "reason": "local", "test_path": False}]))
        self.assertFalse(verdict["ok"])
        self.assertIn("critic", verdict["blockers"][0]["reason"])

    def test_render_points_at_file_line(self) -> None:
        verdict = anti_mock_gate.judge(facts_with(
            [{"file": "web/app.js", "line": 3, "pattern": "mock-id",
              "match": "mock-abc", "reason": "hardcoded", "test_path": False}]))
        report = anti_mock_gate.render(verdict)
        self.assertIn("web/app.js:3", report)
        self.assertIn("BLOCKED", report)


class TestCli(unittest.TestCase):
    def test_cli_exit_codes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            clean = session_facts.save_facts(state, facts_with([]))
            self.assertEqual(anti_mock_gate.main([str(clean)]), 0)
            dirty = session_facts.save_facts(
                state, facts_with(
                    [{"file": "a.js", "line": 1, "pattern": "mock-id",
                      "match": "m", "reason": "r", "test_path": False}]))
            self.assertEqual(anti_mock_gate.main([str(dirty)]), 1)
            self.assertEqual(anti_mock_gate.main([str(state / "no.json")]), 2)


if __name__ == "__main__":
    unittest.main()
