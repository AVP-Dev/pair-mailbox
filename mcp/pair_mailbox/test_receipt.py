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


class TestReceiptRoundBinding(unittest.TestCase):
    """Round 94 D1/D2: the receipt names its own round's subject and tasks."""

    def setUp(self) -> None:
        self.repo = TempRepo()

    def tearDown(self) -> None:
        self.repo.cleanup()

    def _state(self) -> dict:
        return {"round": 94, "task": "STALE legacy string from round 88",
                "writer": "omp", "roles": {"omp": {}, "agy": {}},
                "tasks": [], "evidence": [], "mode": "fix", "autonomy": "auto"}

    def test_subject_from_closed_tasks_not_legacy_string(self) -> None:
        """D1: the Task line names the round's done task, never state['task']."""
        st = self._state()
        st["tasks"] = [{"id": 107, "text": "real round-94 work", "status": "done",
                        "round": 94,
                        "reason": "", "closed_at": "t", "closed_by": "omp"}]
        target = receipt.generate_receipt(self.repo.state, st)
        text = target.read_text(encoding="utf-8")
        self.assertIn("real round-94 work", text)
        self.assertNotIn("STALE legacy string", text)

    def test_tasks_bound_by_close_round(self) -> None:
        """D2: added in N, closed in N+1 — appears in RECEIPT-(N+1)."""
        st = self._state()
        st["tasks"] = [{"id": 101, "text": "added in 93, closed in 94",
                        "status": "done", "round": 93, "closed_round": 94,
                        "reason": "", "closed_at": "t", "closed_by": "omp"}]
        text = receipt.generate_receipt(self.repo.state, st).read_text(encoding="utf-8")
        self.assertIn("#101", text)
        self.assertNotIn("no tasks bound", text)

    def test_old_tasks_fall_back_to_add_round(self) -> None:
        """Pre-D2 records carry no closed_round: honest fallback, not invention."""
        st = self._state()
        st["tasks"] = [{"id": 99, "text": "closed before D2", "status": "done",
                        "round": 94, "reason": "", "closed_at": "t", "closed_by": "omp"}]
        text = receipt.generate_receipt(self.repo.state, st).read_text(encoding="utf-8")
        self.assertIn("#99", text)


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

    def test_receipt_names_the_commit_it_closed(self) -> None:
        """Round 103: the receipt is the next round's record of this one.

        The close path called generate_receipt without head_commit, so round
        103's receipt recorded `Head Commit: —` above twelve commits and a
        merged PR. Anyone reading the receipt could not tell which commit the
        round ended on. With a repo available the sha must land.
        """
        head = git(self.repo.root, "rev-parse", "HEAD")
        target = receipt.generate_receipt(self.repo.state, state_for(),
                                          head_commit=head)
        text = target.read_text(encoding="utf-8")
        self.assertIn(f"**Head Commit**: {head}", text)
        self.assertNotIn("**Head Commit**: —", text)

    def test_receipt_survives_a_missing_head(self) -> None:
        """No sha must not lose the receipt: em-dash, but the rest still lands."""
        target = receipt.generate_receipt(self.repo.state, state_for(),
                                          head_commit="")
        text = target.read_text(encoding="utf-8")
        self.assertIn("**Head Commit**: —", text)
        self.assertIn("## Ground Truth", text)


if __name__ == "__main__":
    unittest.main()
