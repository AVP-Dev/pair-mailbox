"""Unit-тесты для модуля review_gate.py (машинный гейт слияния PR — раунд 60)."""
from __future__ import annotations

import argparse
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from pair_test_base import PairTestCase
import review_gate


class TestReviewGate(PairTestCase):
    def test_check_pr_merge_not_git_repo(self) -> None:
        """Fail-closed: вне git репозитория слияние PR категорически запрещено."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            store = MagicMock()
            store.read.return_value = {"phase": "verify"}
            ok, reason = review_gate.check_pr_merge(store, tmp_path)
            self.assertFalse(ok)
            self.assertIn("cannot verify: not a git repo", reason)

    def test_check_pr_merge_wrong_phase(self) -> None:
        """Слияние разрешено строго на фазе verify."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            # Создаем валидный git репозиторий
            subprocess.run(["git", "init"], cwd=str(tmp_path), check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "pair@test.local"], cwd=str(tmp_path), check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Pair Test"], cwd=str(tmp_path), check=True, capture_output=True)
            (tmp_path / "file.txt").write_text("hello", encoding="utf-8")
            subprocess.run(["git", "add", "file.txt"], cwd=str(tmp_path), check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=str(tmp_path), check=True, capture_output=True)

            store = MagicMock()
            for bad_phase in ["implement", "plan", "scoping", "done"]:
                store.read.return_value = {"phase": bad_phase}
                ok, reason = review_gate.check_pr_merge(store, tmp_path)
                self.assertFalse(ok)
                self.assertIn(f"phase is {bad_phase!r}", reason)

    def test_check_pr_merge_no_test_evidence(self) -> None:
        """Отказ, если в текущем раунде нет успешного test evidence."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            subprocess.run(["git", "init"], cwd=str(tmp_path), check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "pair@test.local"], cwd=str(tmp_path), check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Pair Test"], cwd=str(tmp_path), check=True, capture_output=True)
            (tmp_path / "file.txt").write_text("hello", encoding="utf-8")
            subprocess.run(["git", "add", "file.txt"], cwd=str(tmp_path), check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=str(tmp_path), check=True, capture_output=True)

            store = MagicMock()
            store.read.return_value = {
                "phase": "verify",
                "round": 60,
                "evidence": [{"round": 60, "kind": "lint", "ok": True}],
                "verdicts": [{"role": "omp", "kind": "verdict", "rev": {}}],
            }
            ok, reason = review_gate.check_pr_merge(store, tmp_path)
            self.assertFalse(ok)
            self.assertIn("no passing test evidence", reason)

    def test_check_pr_merge_self_approval_forbidden(self) -> None:
        """УСЛОВИЕ 2: Запрет self-approval писателем."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            subprocess.run(["git", "init"], cwd=str(tmp_path), check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "pair@test.local"], cwd=str(tmp_path), check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Pair Test"], cwd=str(tmp_path), check=True, capture_output=True)
            (tmp_path / "file.txt").write_text("hello", encoding="utf-8")
            subprocess.run(["git", "add", "file.txt"], cwd=str(tmp_path), check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=str(tmp_path), check=True, capture_output=True)

            head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(tmp_path), capture_output=True, text=True).stdout.strip()

            store = MagicMock()
            store.read.return_value = {
                "phase": "verify",
                "round": 60,
                "writer": "agy",
                "evidence": [{"round": 60, "kind": "test", "ok": True}],
                "verdicts": [{
                    "role": "agy",  # Автор вердикта = писатель!
                    "kind": "verdict",
                    "rev": {"head": head, "dirty": "", "files": ["file.txt"]},
                }],
            }
            ok, reason = review_gate.check_pr_merge(store, tmp_path)
            self.assertFalse(ok)
            self.assertIn("self-approval forbidden", reason)

    def test_check_pr_merge_stale_verdict_refused(self) -> None:
        """УСЛОВИЕ 1: Отказ при stale вердикте через review.is_stale."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            subprocess.run(["git", "init"], cwd=str(tmp_path), check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "pair@test.local"], cwd=str(tmp_path), check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Pair Test"], cwd=str(tmp_path), check=True, capture_output=True)
            (tmp_path / "file.txt").write_text("hello", encoding="utf-8")
            subprocess.run(["git", "add", "file.txt"], cwd=str(tmp_path), check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=str(tmp_path), check=True, capture_output=True)

            old_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(tmp_path), capture_output=True, text=True).stdout.strip()

            # Добавляем новый коммит в тот же файл -> вердикт становится stale
            (tmp_path / "file.txt").write_text("hello world", encoding="utf-8")
            subprocess.run(["git", "commit", "-am", "second commit"], cwd=str(tmp_path), check=True, capture_output=True)

            store = MagicMock()
            store.read.return_value = {
                "phase": "verify",
                "round": 60,
                "writer": "agy",
                "evidence": [{"round": 60, "kind": "test", "ok": True}],
                "verdicts": [{
                    "role": "omp",
                    "kind": "verdict",
                    "rev": {"head": old_head, "dirty": "", "files": ["file.txt"]},
                }],
            }
            ok, reason = review_gate.check_pr_merge(store, tmp_path)
            self.assertFalse(ok)
            self.assertIn("verdict is stale", reason)

    def test_check_pr_merge_success(self) -> None:
        """Успешный пропуск гейта при чистом актуальном approve от критика."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            subprocess.run(["git", "init"], cwd=str(tmp_path), check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "pair@test.local"], cwd=str(tmp_path), check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Pair Test"], cwd=str(tmp_path), check=True, capture_output=True)
            (tmp_path / "file.txt").write_text("hello", encoding="utf-8")
            subprocess.run(["git", "add", "file.txt"], cwd=str(tmp_path), check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=str(tmp_path), check=True, capture_output=True)

            head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(tmp_path), capture_output=True, text=True).stdout.strip()

            store = MagicMock()
            store.read.return_value = {
                "phase": "verify",
                "round": 60,
                "writer": "agy",
                "evidence": [{"round": 60, "kind": "test", "ok": True}],
                "verdicts": [{
                    "role": "omp",
                    "kind": "verdict",
                    "rev": {"head": head, "dirty": "", "files": ["file.txt"]},
                }],
            }
            store.notes_root = tmp_path / "notes"
            store.notes_root.mkdir()
            (store.notes_root / "verdict.md").write_text("## 2026-10-12T01:00:00 - omp\n\nВЕРДИКТ: APPROVE / OK", encoding="utf-8")
            ok, reason = review_gate.check_pr_merge(store, tmp_path)
            self.assertTrue(ok)
            self.assertEqual(reason, "")

    def test_check_pr_merge_reject_verdict_refused(self) -> None:
        """Отказ, если в тексте вердикта зафиксирован отказ/reject."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            subprocess.run(["git", "init"], cwd=str(tmp_path), check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "pair@test.local"], cwd=str(tmp_path), check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Pair Test"], cwd=str(tmp_path), check=True, capture_output=True)
            (tmp_path / "file.txt").write_text("hello", encoding="utf-8")
            subprocess.run(["git", "add", "file.txt"], cwd=str(tmp_path), check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=str(tmp_path), check=True, capture_output=True)
            head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(tmp_path), capture_output=True, text=True).stdout.strip()

            store = MagicMock()
            store.read.return_value = {
                "phase": "verify",
                "round": 60,
                "writer": "agy",
                "evidence": [{"round": 60, "kind": "test", "ok": True}],
                "verdicts": [{
                    "role": "omp",
                    "kind": "verdict",
                    "rev": {"head": head, "dirty": "", "files": ["file.txt"]},
                }],
            }
            store.notes_root = tmp_path / "notes"
            store.notes_root.mkdir()
            (store.notes_root / "verdict.md").write_text("## 2026-10-12T01:00:00 - omp\n\nВЕРДИКТ: ОТКАЗ. Изменения отклонены.", encoding="utf-8")
            ok, reason = review_gate.check_pr_merge(store, tmp_path)
            self.assertFalse(ok)
            self.assertIn("rejected the changes", reason)

    def test_check_pr_merge_verdict_without_head_refused(self) -> None:
        """Отказ, если у вердикта нет привязки к ревизии (head пуст)."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            subprocess.run(["git", "init"], cwd=str(tmp_path), check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "pair@test.local"], cwd=str(tmp_path), check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Pair Test"], cwd=str(tmp_path), check=True, capture_output=True)
            (tmp_path / "file.txt").write_text("hello", encoding="utf-8")
            subprocess.run(["git", "add", "file.txt"], cwd=str(tmp_path), check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=str(tmp_path), check=True, capture_output=True)

            store = MagicMock()
            store.read.return_value = {
                "phase": "verify",
                "round": 60,
                "writer": "agy",
                "evidence": [{"round": 60, "kind": "test", "ok": True}],
                "verdicts": [{
                    "role": "omp",
                    "kind": "verdict",
                    "rev": {"head": "", "dirty": "", "files": ["file.txt"]},
                }],
            }
            ok, reason = review_gate.check_pr_merge(store, tmp_path)
            self.assertFalse(ok)
            self.assertIn("no revision binding", reason)

    def test_cmd_pr_force_without_reason_fails(self) -> None:
        """УСЛОВИЕ 3: --force без --reason отклоняется."""
        store = MagicMock()
        args = argparse.Namespace(
            pr_action="merge",
            force=True,
            reason="",
            pr_id="123",
            role="agy",
        )
        res = review_gate.cmd_pr(store, args)
        self.assertEqual(res, 2)
        store.append_transcript.assert_not_called()

    def test_cmd_pr_force_without_human_ok_fails(self) -> None:
        """УСЛОВИЕ 3: --force без human_ok (одобрения человеком в раунде) отклоняется."""
        store = MagicMock()
        store.read.return_value = {"round": 60, "phase": "verify", "human_ok": []}
        args = argparse.Namespace(
            pr_action="merge",
            force=True,
            reason="urgent fix",
            pr_id="123",
            role="agy",
        )
        res = review_gate.cmd_pr(store, args)
        self.assertEqual(res, 1)
        store.append_transcript.assert_not_called()

    @patch("review_gate.run_gh_merge")
    def test_cmd_pr_force_with_reason_and_human_ok_records_audit(self, mock_gh: MagicMock) -> None:
        """УСЛОВИЕ 3: --force с --reason и human_ok логируется в аудит/транскрипт."""
        mock_gh.return_value = (0, "merged")
        store = MagicMock()
        store.read.return_value = {
            "round": 60,
            "phase": "verify",
            "human_ok": [{"round": 60, "phase": "verify"}],
        }
        args = argparse.Namespace(
            pr_action="merge",
            force=True,
            reason="urgent fix approved by human",
            pr_id="123",
            role="human",
        )
        res = review_gate.cmd_pr(store, args)
        self.assertEqual(res, 0)
        store.append_transcript.assert_any_call(
            "pr_merge_forced",
            "human",
            reason="urgent fix approved by human",
            pr_id="123",
        )


if __name__ == "__main__":
    unittest.main()
