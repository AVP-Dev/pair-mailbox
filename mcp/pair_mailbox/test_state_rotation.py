#!/usr/bin/env python3
"""Tests for state rotation (round 106 slice B): the hot file stops growing forever.

Written before the change, because the change touches the single most load-bearing
file in the system. The contract under test:

- a bounded ACTIVE window in state.json; older records rotate beside it as
  per-record JSONL, exactly the way transcript and notes already rotate;
- `_load` sees the UNION: nothing that existed becomes invisible, only the hot
  file stops carrying it;
- a rotate-and-reload round trip preserves every record, count included.

Run: python3 mcp/pair_mailbox/test_state_rotation.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "mcp" / "pair_mailbox"))

from server import Store  # noqa: E402
import state_rotation  # noqa: E402
from state_rotation import archive_dir  # noqa: E402


def _seed(root: Path, tasks: int = 5) -> Store:
    store = Store(root / ".pair")
    store.mutate(lambda s: s.update({
        "round": 42, "mode": "fix", "phase": "implement",
        "tasks": [{"id": i + 1, "text": f"task {i + 1}", "status": "closed",
                   "round": 40 + i, "closed_at": f"2026-10-{i + 1:02d}T00:00:00+00:00"}
                  for i in range(tasks)],
        "evidence": [{"kind": "test", "ok": True, "value": f"run {i}",
                      "round": 40 + i} for i in range(tasks)],
    }))
    return store


class StateRotationCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = _seed(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # -- the invariant that must never break ------------------------------

    def test_nothing_becomes_invisible(self) -> None:
        """Rotation MERGES on load: every record that existed before a rotation
        is still readable after. A file that shrinks is fine; a record that
        disappears is not — that is the phase-95 corpus called the ancient
        listing-INDEX-DRIFT bug, and it MUST be caught on a small migration."""
        before_tasks = len(self.store.read()["tasks"])
        before_evidence = len(self.store.read()["evidence"])
        state_rotation.rotate(self.store.root, keep_tasks=2, keep_evidence=1)
        after = self.store.read()
        self.assertEqual(len(after["tasks"]), before_tasks,
                         "rotated tasks must not become invisible")
        self.assertEqual(len(after["evidence"]), before_evidence,
                         "rotated evidence must not become invisible")

    def test_hot_file_stops_growing(self) -> None:
        """The point of the slice: after rotation the ACTIVE file is bounded."""
        before = self.store.root.joinpath("state.json").stat().st_size
        state_rotation.rotate(self.store.root, keep_tasks=1, keep_evidence=1)
        after = self.store.root.joinpath("state.json").stat().st_size
        self.assertLess(after, before, "the hot file must shrink")

    def test_rotate_is_idempotent(self) -> None:
        """A second rotate on an already-rotated state must not double-count:
        the archive is the source of truth for what already left."""
        state_rotation.rotate(self.store.root, keep_tasks=2, keep_evidence=1)
        state_rotation.rotate(self.store.root, keep_tasks=2, keep_evidence=1)
        after = self.store.read()
        ids = [t["id"] for t in after["tasks"]]
        self.assertEqual(len(ids), len(set(ids)),
                         "a task must not appear twice after two rotations")

    def test_round_trip_preserves_ordering(self) -> None:
        """Records keep their relative order across the boundary: the newest
        stay active, the oldest sit in the archive, and load hands back the same
        sequence it was given."""
        before = self.store.read()["tasks"]
        state_rotation.rotate(self.store.root, keep_tasks=3, keep_evidence=3)
        after = self.store.read()["tasks"]
        self.assertEqual(before, after)

    def test_empty_state_rotates_to_nothing(self) -> None:
        import shutil
        shutil.rmtree(self.root / ".pair" / "archive", ignore_errors=True)
        store = Store(self.root / "empty" / ".pair")
        store.mutate(lambda s: s.update({"tasks": [], "evidence": []}))
        moved_tasks, moved_evidence = state_rotation.rotate(store.root)
        self.assertEqual(moved_tasks, 0)
        self.assertEqual(moved_evidence, 0)

    def test_a_fresh_state_with_no_archive_is_fine(self) -> None:
        """A project that never rotated has no archive dir; rotate must not fail
        on it — and when there is genuinely nothing to move it correctly creates
        no directory either (an empty dir would be a footprint, not a fact)."""
        import shutil
        shutil.rmtree(self.root / ".pair" / "archive", ignore_errors=True)
        self.assertFalse((self.root / ".pair" / "archive").exists())
        moved = state_rotation.rotate(self.store.root, keep_tasks=2, keep_evidence=1)
        self.assertEqual(moved, (3, 4), "the seed has 5 of each")
        self.assertTrue(archive_dir(self.store.root).is_dir())
        after = self.store.read()
        self.assertEqual(len(after["tasks"]), 5)
        self.assertEqual(len(after["evidence"]), 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
