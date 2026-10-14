"""Unit tests for review_gate.py (the PR-merge machine gate — round 60)."""
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
        """Fail-closed: outside a git repo the PR merge stays firmly banned."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            store = MagicMock()
            store.read.return_value = {"phase": "verify"}
            ok, reason = review_gate.check_pr_merge(store, tmp_path)
            self.assertFalse(ok)
            self.assertIn("cannot verify: not a git repo", reason)

    def test_check_pr_merge_wrong_phase(self) -> None:
        """Merging allowed strictly in the verify phase."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            # Create a valid git repo
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
        """Refuse when the current round holds no successful test evidence."""
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
        """CONDITION 2: writer self-approval ban."""
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
                    "role": "agy",  # The verdict author IS the writer!
                    "kind": "verdict",
                    "rev": {"head": head, "dirty": "", "files": ["file.txt"]},
                }],
            }
            ok, reason = review_gate.check_pr_merge(store, tmp_path)
            self.assertFalse(ok)
            self.assertIn("self-approval forbidden", reason)

    def test_check_pr_merge_stale_verdict_refused(self) -> None:
        """CONDITION 1: refuse on a stale verdict via review.is_stale."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            subprocess.run(["git", "init"], cwd=str(tmp_path), check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "pair@test.local"], cwd=str(tmp_path), check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Pair Test"], cwd=str(tmp_path), check=True, capture_output=True)
            (tmp_path / "file.txt").write_text("hello", encoding="utf-8")
            subprocess.run(["git", "add", "file.txt"], cwd=str(tmp_path), check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=str(tmp_path), check=True, capture_output=True)

            old_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(tmp_path), capture_output=True, text=True).stdout.strip()

            # Add a new commit to the same file -> the verdict turns stale
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
            store.notes_root = tmp_path / "notes"
            store.notes_root.mkdir()
            (store.notes_root / "verdict.md").write_text(f"## 2026-10-12T01:00:00 - omp\n\nverdict-approve {old_head[:12]}", encoding="utf-8")
            ok, reason = review_gate.check_pr_merge(store, tmp_path)
            self.assertFalse(ok)
            self.assertIn("verdict is stale", reason)

    def test_check_pr_merge_success(self) -> None:
        """A clean pass on a fresh critic approve."""
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
            (store.notes_root / "verdict.md").write_text(f"## 2026-10-12T01:00:00 - omp\n\nverdict-approve {head[:12]}", encoding="utf-8")
            ok, reason = review_gate.check_pr_merge(store, tmp_path)
            self.assertTrue(ok)
            self.assertEqual(reason, "")

    def test_check_pr_merge_uncommitted_untracked_refused(self) -> None:
        """U3: merge refused with uncommitted/untracked files in the work tree."""
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
            (store.notes_root / "verdict.md").write_text(f"## 2026-10-12T01:00:00 - omp\n\nverdict-approve {head[:12]}", encoding="utf-8")

            # Create an untracked code/scratch file
            (tmp_path / "untracked.py").write_text("print(1)", encoding="utf-8")

            ok, reason = review_gate.check_pr_merge(store, tmp_path)
            self.assertFalse(ok)
            self.assertIn("working tree has uncommitted changes", reason)
            self.assertIn("untracked.py", reason)

    def test_check_pr_merge_reject_verdict_refused(self) -> None:
        """Refuse when the verdict text pins a refusal/reject."""
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
        """Refuse when the verdict binds to no revision (empty head)."""
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

    def test_check_pr_merge_prose_trap_words_refused(self) -> None:
        """BLOCKER 1 (omp): a refusal-flavoured verdict with trap words (hook, book, lock) is rejected."""
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
            # Critic text with no approve marker, holding 'hook' (contains 'ok')
            (store.notes_root / "verdict.md").write_text(
                "## 2026-10-12T01:00:00 - omp\n\nТребуется доработка: в hook пайплайна есть гонка.",
                encoding="utf-8",
            )
            ok, reason = review_gate.check_pr_merge(store, tmp_path)
            self.assertFalse(ok)
            self.assertIn("last verdict is not approved", reason)

    def test_check_pr_merge_missing_verdict_file_refused(self) -> None:
        """BLOCKER 2 (omp): fail-closed on a missing notes/verdict.md."""
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
            # verdict.md missing
            ok, reason = review_gate.check_pr_merge(store, tmp_path)
            self.assertFalse(ok)
            self.assertIn("notes/verdict.md does not exist", reason)

    def test_check_pr_merge_prose_ne_odobreno_refused(self) -> None:
        """D1 (omp): the natural refusal wording is rejected."""
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
            (store.notes_root / "verdict.md").write_text(
                "## 2026-10-12T01:00:00 - omp\n\nВЕРДИКТ по раунду 60: не одобрено, есть замечания.",
                encoding="utf-8",
            )
            ok, reason = review_gate.check_pr_merge(store, tmp_path)
            self.assertFalse(ok)
            self.assertTrue("rejected the changes" in reason or "not approved" in reason)

    def test_check_pr_merge_marker_head_mismatch_refused(self) -> None:
        """D2 (omp): a marker with a mismatched HEAD (e.g. deadbeef1234) is rejected."""
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
            (store.notes_root / "verdict.md").write_text(
                "## 2026-10-12T01:00:00 - omp\n\nverdict-approve deadbeef1234\n\nВсе хорошо.",
                encoding="utf-8",
            )
            ok, reason = review_gate.check_pr_merge(store, tmp_path)
            self.assertFalse(ok)
            self.assertIn("marker head 'deadbeef1234' does not match current HEAD", reason)

    def test_cmd_pr_force_without_reason_fails(self) -> None:
        """CONDITION 3: --force without --reason is rejected."""
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
        """CONDITION 3: --force without human_ok (human approval in the round) is rejected."""
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
        """CONDITION 3: --force with --reason and human_ok logs into audit/transcript."""
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

    def _repo_with_commit(self, tmp: Path) -> str:
        subprocess.run(["git", "init"], cwd=str(tmp), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "pair@test.local"], cwd=str(tmp), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Pair Test"], cwd=str(tmp), check=True, capture_output=True)
        (tmp / "file.txt").write_text("hello", encoding="utf-8")
        subprocess.run(["git", "add", "file.txt"], cwd=str(tmp), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=str(tmp), check=True, capture_output=True)
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(tmp), capture_output=True, text=True).stdout.strip()

    def _state(self, head: str, kind: str = "verdict-round91-critic") -> dict:
        return {
            "phase": "verify",
            "round": 91,
            "writer": "omp",
            "evidence": [{"round": 91, "kind": "test", "ok": True}],
            "verdicts": [{
                "role": "opencode",
                "kind": kind,
                "rev": {"head": head, "dirty": "", "files": []},
            }],
        }

    def test_house_slug_kind_passes(self) -> None:
        """Round 91: the project writes verdict-* slugs — the gate reads them."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            head = self._repo_with_commit(tmp_path)
            store = MagicMock()
            store.read.return_value = self._state(head)
            store.notes_root = tmp_path / "notes"
            store.notes_root.mkdir()
            (store.notes_root / "verdict-round91-critic.md").write_text(
                f"## t — opencode\n\n**APPROVED** on {head[:12]}\n", encoding="utf-8")
            ok, reason = review_gate.check_pr_merge(store, tmp_path)
            self.assertTrue(ok, reason)

    def test_house_needs_work_refuses(self) -> None:
        """**NEEDS WORK** refuses; bare prose never counts either way."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            head = self._repo_with_commit(tmp_path)
            store = MagicMock()
            store.read.return_value = self._state(head)
            store.notes_root = tmp_path / "notes"
            store.notes_root.mkdir()
            (store.notes_root / "verdict-round91-critic.md").write_text(
                "## t — opencode\n\n**NEEDS WORK.** nope\n", encoding="utf-8")
            ok, reason = review_gate.check_pr_merge(store, tmp_path)
            self.assertFalse(ok)
            self.assertIn("rejected", reason)
            (store.notes_root / "verdict-round91-critic.md").write_text(
                "## t — opencode\n\nlooks good to me\n", encoding="utf-8")
            ok, _ = review_gate.check_pr_merge(store, tmp_path)
            self.assertFalse(ok)

    def test_slug_file_missing_falls_back(self) -> None:
        """No verdict-<slug>.md: the old verdict.md still judges (no crash)."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            head = self._repo_with_commit(tmp_path)
            store = MagicMock()
            store.read.return_value = self._state(head)
            store.notes_root = tmp_path / "notes"
            store.notes_root.mkdir()
            (store.notes_root / "verdict.md").write_text(
                f"## t — opencode\n\nverdict-approve {head[:12]}\n", encoding="utf-8")
            ok, reason = review_gate.check_pr_merge(store, tmp_path)
            self.assertTrue(ok, reason)


if __name__ == "__main__":
    unittest.main()
