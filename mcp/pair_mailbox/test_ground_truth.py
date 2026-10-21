"""Tests for verify-ground-truth.py: an honest summary passes, a lying one blocks.

The script lives in scripts/, no suite file beside it - coverage lives right here by the
"one test file per source" rule, calling over subprocess.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import session_facts

GATE = Path(__file__).resolve().parent.parent.parent / "scripts" / "verify-ground-truth.py"
REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def facts_with(**over: object) -> dict:
    base: dict = {"version": session_facts.FACTS_VERSION, "head": "h" * 40,
                  "dirty_hash": "d" * 16, "touched": ["a.py"],
                  "untracked": [], "diff_stat": "", "mock_flags": [],
                  "test_runs": [{"command": "pytest", "exit": 0}]}
    base.update(over)
    return base


class TestGroundTruthGate(unittest.TestCase):
    def run_gate(self, summary: dict, facts: dict) -> tuple[int, str]:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            summary_path = state / "summary.json"
            summary_path.write_text(json.dumps(summary), encoding="utf-8")
            facts_path = session_facts.save_facts(state, facts)
            proc = subprocess.run(
                ["python3", str(GATE), str(summary_path), str(facts_path)],
                capture_output=True, text=True, timeout=30, cwd=REPO_ROOT)
            return proc.returncode, proc.stdout

    def test_honest_passes(self) -> None:
        facts = facts_with()
        code, out = self.run_gate(
            {"head": "h" * 40, "dirty_hash": "d" * 16,
             "files": ["a.py"], "tests": [{"command": "pytest", "exit": 0}]},
            facts)
        self.assertEqual(code, 0)
        self.assertIn("PASS", out)

    def test_ghost_file_blocked(self) -> None:
        code, out = self.run_gate({"files": ["ghost.py"]}, facts_with())
        self.assertEqual(code, 1)
        self.assertIn("ghost.py", out)

    def test_unrun_test_blocked(self) -> None:
        code, out = self.run_gate(
            {"tests": [{"command": "never-ran", "exit": 0}]}, facts_with())
        self.assertEqual(code, 1)
        self.assertIn("never-ran", out)

    def test_stale_head_blocked(self) -> None:
        code, out = self.run_gate({"head": "0" * 40}, facts_with())
        self.assertEqual(code, 1)
        self.assertIn("head", out)

    def test_broken_facts_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            summary_path = state / "summary.json"
            summary_path.write_text("{}", encoding="utf-8")
            bad = state / "tree-facts.json"
            bad.write_text("{nope", encoding="utf-8")
            proc = subprocess.run(
                ["python3", str(GATE), str(summary_path), str(bad)],
                capture_output=True, text=True, timeout=30, cwd=REPO_ROOT)
            self.assertEqual(proc.returncode, 2)


if __name__ == "__main__":
    unittest.main()
