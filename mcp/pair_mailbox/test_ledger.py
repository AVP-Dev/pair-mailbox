"""Tests for ledger.py: 5 slices, empty fields stay empty, tail by round.

One test file per source: `ledger.py` -> `test_ledger.py`
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import ledger


class TestLedger(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_append_keeps_five_slices(self) -> None:
        record = ledger.append(self.state_dir, 81, "omp",
                               {"decisions": ["s1 over mcp/"],
                                "evidence": ["9 passed, exit 0"]})
        for name in ledger.SLICES:
            self.assertIn(name, record)
        self.assertEqual(record["decisions"], ["s1 over mcp/"])
        self.assertEqual(record["touchpoints"], [])

    def test_unknown_keys_dropped(self) -> None:
        record = ledger.append(self.state_dir, 81, "omp",
                               {"decisions": [], "invention": ["x"]})
        self.assertNotIn("invention", record)

    def test_read_round_filters(self) -> None:
        ledger.append(self.state_dir, 80, "omp", {"decisions": ["old"]})
        ledger.append(self.state_dir, 81, "agy", {"decisions": ["new"]})
        got = ledger.read_round(self.state_dir, 81)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["decisions"], ["new"])

    def test_missing_file_empty(self) -> None:
        self.assertEqual(ledger.read_round(self.state_dir, 81), [])


if __name__ == "__main__":
    unittest.main()
