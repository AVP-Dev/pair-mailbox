"""Tests for receipt.py Ground Truth (R82 S1): the facts section + fail-closed.

One test file per source: `receipt.py` -> `test_receipt.py`.
"""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

import receipt
from receipt import GroundTruthBlocked


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=repo,
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


class TempRepo:
    """An empty git repo with a .pair state dir."""

    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        git(self.root, "init", "-q")
        git(self.root, "config", "user.email", "t@t")
        git(self.root, "config", "user.name", "t")
        (self.root / "base.txt").write_text("base\n", encoding="utf-8")
        git(self.root, "add", "base.txt")
        git(self.root, "commit", "-qm", "base")
        self.state = self.root / ".pair"
        self.state.mkdir()

    def cleanup(self) -> None:
        self.tmp.cleanup()


def state_for(round_number: int = 1) -> dict:
    return {"round": 1, "task": "t", "writer": "omp",
            "roles": {"omp": {}, "agy": {}}, "tasks": [], "evidence": [],
            "mode": "fix", "autonomy": "auto"}


class TestReceiptGroundTruth(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = TempRepo()

    def tearDown(self) -> None:
        self.repo.cleanup()

    def test_clean_tree_receipt_has_ground_truth(self) -> None:
        target = receipt.generate_receipt(self.repo.state, state_for())
        text = target.read_text(encoding="utf-8")
        self.assertIn("## Ground Truth", text)
        self.assertIn("dirty_hash:", text)
        self.assertIn("anti-mock: PASS", text)

    def test_combat_mock_blocks_receipt(self) -> None:
        bad = self.repo.root / "web" / "app.js"
        bad.parent.mkdir()
        bad.write_text("const id = 'mock-abc123';\n", encoding="utf-8")
        git(self.repo.root, "add", "web/app.js")
        git(self.repo.root, "commit", "-qm", "web")
        bad.write_text("const a = 'mock-abc123';\nconst b = INITIAL_X;\n",
                       encoding="utf-8")
        with self.assertRaises(GroundTruthBlocked):
            receipt.generate_receipt(self.repo.state, state_for())
        self.assertFalse((self.repo.state / "RECEIPT-1.md").exists())


if __name__ == "__main__":
    unittest.main()
