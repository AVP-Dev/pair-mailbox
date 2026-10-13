"""Тесты для round_summary.py: выжимка только компонует, не выдумывает."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import ledger
import round_summary
import session_facts


def make_facts(**over: object) -> dict:
    base: dict = {
        "version": session_facts.FACTS_VERSION,
        "head": "a" * 40,
        "dirty_hash": "b" * 16,
        "touched": ["x.py"],
        "untracked": [],
        "mock_flags": [{"pattern": "stub-body", "file": "x.py"}],
        "test_runs": [{"command": "t", "exit_code": 0}],
    }
    base.update(over)
    return base


class TestBuild(unittest.TestCase):
    def test_aggregates_slices_without_invention(self) -> None:
        verdict = {"ok": True, "blockers": [], "warnings": [{"x": 1}]}
        records = [{"decisions": ["d1"], "evidence": ["e1", "e2"]}]
        summary = round_summary.build(make_facts(), records, verdict)
        self.assertEqual(summary["ledger"]["decisions"], ["d1"])
        self.assertEqual(summary["ledger"]["evidence"], ["e1", "e2"])
        self.assertEqual(summary["ledger"]["debts"], [])
        self.assertEqual(summary["anti_mock"],
                         {"ok": True, "blockers": 0, "warnings": 1})
        self.assertEqual(len(summary["mock_flags"]), 1)

    def test_empty_inputs_empty_summary(self) -> None:
        summary = round_summary.build(
            make_facts(touched=[], mock_flags=[], test_runs=[]), [],
            {"ok": False, "blockers": [{"x": 1}], "warnings": []})
        for name in ledger.SLICES:
            self.assertEqual(summary["ledger"][name], [])
        self.assertFalse(summary["anti_mock"]["ok"])

    def test_render_counts_match(self) -> None:
        summary = round_summary.build(
            make_facts(), [{"decisions": ["d1"]}],
            {"ok": True, "blockers": [], "warnings": []})
        text = round_summary.render(summary)
        self.assertIn("touched: 1", text)
        self.assertIn("mock-flags: 1", text)
        self.assertIn("decisions: 1", text)
        self.assertIn("anti-mock: PASS", text)


class TestCli(unittest.TestCase):
    def test_cli_reads_ledger_round(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            facts = make_facts()
            facts_path = state / "facts.json"
            facts_path.write_text(json.dumps(facts), encoding="utf-8")
            ledger.append(state, 7, "omp", {"decisions": ["picked x"]})
            ledger.append(state, 8, "omp", {"decisions": ["other"]})
            out = state / "summary.json"
            code = round_summary.main(
                ["--facts", str(facts_path), "--state-dir", str(state),
                 "--round", "7", "--out", str(out)])
            self.assertEqual(code, 0)
            saved = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(saved["ledger"]["decisions"], ["picked x"])

    def test_cli_missing_facts_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code = round_summary.main(
                ["--facts", str(Path(tmp) / "no.json"),
                 "--state-dir", tmp, "--round", "1"])
            self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
