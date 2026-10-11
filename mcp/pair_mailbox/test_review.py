#!/usr/bin/env python3
"""Привязка вердиктов к ревизии и лимит очереди (review-queue-and-stale-verdicts).

Вердикт относится к HEAD + dirty-hash; stale — только при пересечении
файлов; очередь — не более 2; done блокируется поверх pending/stale.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pair_test_base import PairTestCase  # noqa: E402


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True,
                   capture_output=True, text=True, timeout=10)


class TestReviewPure(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        git(self.repo, "init")
        git(self.repo, "config", "user.email", "t@t.t")
        git(self.repo, "config", "user.name", "t")
        (self.repo / "a.py").write_text("x=1\n", encoding="utf-8")
        git(self.repo, "add", "a.py")
        git(self.repo, "commit", "-m", "init")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_stale_when_same_files_changed(self) -> None:
        """Тот же файл переписан поверх — вердикт stale."""
        import review
        old = review.snapshot(self.repo)
        self.assertTrue(old["head"])
        (self.repo / "a.py").write_text("x=2\n", encoding="utf-8")
        git(self.repo, "add", "a.py")
        git(self.repo, "commit", "-m", "bump a")
        current = review.git_head(self.repo)
        stale, why = review.is_stale(old, current,
                                     review.changed_between(self.repo, old["head"], current),
                                     review.dirty_files(self.repo))
        self.assertTrue(stale, f"тот же файл уехал, обязано быть stale: {why}")

    def test_fresh_when_unrelated_files_changed(self) -> None:
        """Писатель ушёл в другой модуль — вердикт не stale (dirty overlap)."""
        import review
        old = review.snapshot(self.repo)
        (self.repo / "b.py").write_text("y=1\n", encoding="utf-8")
        git(self.repo, "add", "b.py")
        git(self.repo, "commit", "-m", "add b")
        current = review.git_head(self.repo)
        stale, _ = review.is_stale(old, current,
                                   review.changed_between(self.repo, old["head"], current),
                                   review.dirty_files(self.repo))
        self.assertFalse(stale, "чужие файлы не инвалидируют вердикт")

    def test_fail_open_without_git(self) -> None:
        import review
        with tempfile.TemporaryDirectory() as tmp:
            plain = Path(tmp)
            snap = review.snapshot(plain)
            self.assertEqual("", snap["head"])
            stale, _ = review.is_stale({"head": "abc", "files": ["a.py"]}, "", [], [])
            self.assertFalse(stale, "без git раунд держать нельзя")

    def test_dirty_files_filters_untracked_by_default(self) -> None:
        """У1: dirty_files по умолчанию не возвращает untracked файлы (??)."""
        import review
        (self.repo / "untracked_card.md").write_text("backlog card\n", encoding="utf-8")
        self.assertEqual([], review.dirty_files(self.repo))
        self.assertEqual(["untracked_card.md"], review.dirty_files(self.repo, include_untracked=True))

    def test_snapshot_includes_head_files_with_untracked_scratch(self) -> None:
        """У1: snapshot() объединяет head_files ∪ tracked-dirty, не вытесняя файлы коммита скретч-файлами."""
        import review
        (self.repo / "scratch.txt").write_text("scratch\n", encoding="utf-8")
        snap = review.snapshot(self.repo)
        self.assertIn("a.py", snap["files"], "файлы коммита обязаны быть в coverage снимка")
        self.assertNotIn("scratch.txt", snap["files"], "untracked скретч не должен загрязнять snapshot")

    def test_is_stale_untracked_scratch_does_not_invalidate(self) -> None:
        """У2(а): untracked скретч-файл, появившийся после вердикта, НЕ делает вердикт stale."""
        import review
        old = review.snapshot(self.repo)
        current = review.git_head(self.repo)
        (self.repo / "scratch.txt").write_text("new untracked\n", encoding="utf-8")
        stale, reason = review.is_stale(
            old, current,
            review.changed_between(self.repo, old["head"], current),
            review.dirty_files(self.repo),
        )
        self.assertFalse(stale, f"untracked файл не должен вызывать вечный stale: {reason}")

    def test_is_stale_tracked_edit_invalidates(self) -> None:
        """У2(б): реальная tracked-модификация пересмотренного файла делает вердикт stale."""
        import review
        old = review.snapshot(self.repo)
        current = review.git_head(self.repo)
        (self.repo / "a.py").write_text("x=999\n", encoding="utf-8")
        stale, reason = review.is_stale(
            old, current,
            review.changed_between(self.repo, old["head"], current),
            review.dirty_files(self.repo),
        )
        self.assertTrue(stale, "tracked модификация обязана делать вердикт stale")
        self.assertIn("a.py", reason)

    def test_is_stale_tracked_edit_unrelated_does_not_invalidate(self) -> None:
        """У2(в): tracked-модификация несвязанного файла не делает вердикт stale."""
        import review
        (self.repo / "b.py").write_text("b=1\n", encoding="utf-8")
        git(self.repo, "add", "b.py")
        git(self.repo, "commit", "-m", "add b")
        old = {"head": review.git_head(self.repo), "files": ["a.py"]}
        (self.repo / "b.py").write_text("b=2\n", encoding="utf-8")
        current = review.git_head(self.repo)
        stale, _ = review.is_stale(
            old, current,
            review.changed_between(self.repo, old["head"], current),
            review.dirty_files(self.repo),
        )
        self.assertFalse(stale, "tracked модификация стороннего файла не должна инвалидировать вердикт")


class TestReviewServer(PairTestCase):
    def fixed_snapshot(self, head: str = "h" * 40, files: list | None = None):
        return {"head": head, "dirty": "d123", "files": files if files is not None else ["a.py"]}

    def test_obligation_stores_rev(self) -> None:
        import review
        self.writer.tool_pair_state()
        self.peer.tool_pair_state()
        with mock.patch.object(review, "snapshot", return_value=self.fixed_snapshot()):
            self.writer.tool_pair_send(to="reviewer-agent", text="прошу ревью",
                                       expects_reply=True)
        obligations = list(self.store.obligations_root.glob("*.json"))
        self.assertEqual(1, len(obligations))
        record = json.loads(obligations[0].read_text(encoding="utf-8"))
        self.assertIn("rev", record, "запрос обязан фиксировать ревизию")
        self.assertEqual("h" * 40, record["rev"].get("head"))

    def test_queue_limit_refuses_third(self) -> None:
        """Третий неподтверждённый запрос — отказ с указанием на pair_read."""
        import review
        self.writer.tool_pair_state()
        self.peer.tool_pair_state()
        with mock.patch.object(review, "snapshot", return_value=self.fixed_snapshot()):
            self.writer.tool_pair_send(to="reviewer-agent", text="ревью 1", expects_reply=True)
            self.writer.tool_pair_send(to="reviewer-agent", text="ревью 2", expects_reply=True)
            with self.assertRaises(Exception) as caught:
                self.writer.tool_pair_send(to="reviewer-agent", text="ревью 3",
                                           expects_reply=True)
        self.assertIn("очередь", str(caught.exception).lower())

    def test_pending_reviews_block_done(self) -> None:
        import review
        self.writer.tool_pair_state()
        self.peer.tool_pair_state()
        with mock.patch.object(review, "snapshot", return_value=self.fixed_snapshot()):
            self.writer.tool_pair_send(to="reviewer-agent", text="прошу ревью",
                                       expects_reply=True)
        blockers = self.writer._done_blockers(self.store.read())
        self.assertTrue(any("висящих ревью" in b for b in blockers),
                        f"открытая очередь обязана держать done: {blockers}")

    def test_stale_verdict_blocks_done(self) -> None:
        """Новый коммит поверх вердикта по тем же файлам — done заблокирован."""
        import review
        self.writer.tool_pair_state()
        self.peer.tool_pair_state()
        old = self.fixed_snapshot(head="a" * 40, files=["a.py"])
        with mock.patch.object(review, "snapshot", return_value=old):
            self.peer.tool_pair_note(kind="verdict", text="проверено: ок")
        with mock.patch.object(review, "git_head", return_value="b" * 40), \
                mock.patch.object(review, "changed_between", return_value=["a.py"]), \
                mock.patch.object(review, "dirty_files", return_value=[]):
            blockers = self.writer._done_blockers(self.store.read())
        self.assertTrue(any("stale" in b for b in blockers),
                        f"stale-вердикт обязан держать done: {blockers}")

    def test_fresh_verdict_does_not_block_done(self) -> None:
        import review
        self.writer.tool_pair_state()
        self.peer.tool_pair_state()
        snap = self.fixed_snapshot(head="a" * 40, files=["a.py"])
        with mock.patch.object(review, "snapshot", return_value=snap):
            self.peer.tool_pair_note(kind="verdict", text="проверено: ок")
        with mock.patch.object(review, "git_head", return_value="a" * 40), \
                mock.patch.object(review, "changed_between", return_value=[]), \
                mock.patch.object(review, "dirty_files", return_value=[]):
            blockers = [b for b in self.writer._done_blockers(self.store.read())
                        if "stale" in b or "висящих ревью" in b]
        self.assertEqual([], blockers, f"свежий вердикт держать не должен: {blockers}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
