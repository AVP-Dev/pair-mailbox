"""Unit tests for review_gate.py (the PR-merge machine gate — round 60)."""
from __future__ import annotations

import argparse
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
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
                "evidence": [{"round": 60, "kind": "test", "ok": True, "head": head}],
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
            head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(tmp_path), capture_output=True, text=True).stdout.strip()

            store = MagicMock()
            store.read.return_value = {
                "phase": "verify",
                "round": 60,
                "writer": "agy",
                "evidence": [{"round": 60, "kind": "test", "ok": True, "head": head}],
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
                "evidence": [{"round": 60, "kind": "test", "ok": True, "head": head}],
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

    def test_check_pr_merge_refuses_evidence_for_another_head(self) -> None:
        """Round 138: the evidence must name the commit that gets merged.

        The round number does not move when the head does, so a record written
        for one commit kept vouching for every commit after it in that round.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            head = self._repo_with_commit(tmp_path)
            state = self._state(head)
            state["evidence"][0]["head"] = "deadbeef1234567890"
            store = MagicMock()
            store.read.return_value = state
            store.notes_root = tmp_path / "notes"
            store.notes_root.mkdir()
            (store.notes_root / "verdict-round91-critic.md").write_text(
                f"## t — opencode\n\n**APPROVED** on {head[:12]}\n", encoding="utf-8")
            ok, reason = review_gate.check_pr_merge(store, tmp_path)
            self.assertFalse(ok)
            self.assertIn("no passing test evidence names the head being merged", reason)
            self.assertIn("deadbeef1234", reason)

    def test_check_pr_merge_refuses_evidence_without_a_head(self) -> None:
        """Round 138: a fact that names no commit vouches for nothing.

        The record must say which commit it was measured on; an old record that
        says nothing is not allowed to cover a new commit.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            head = self._repo_with_commit(tmp_path)
            state = self._state(head)
            state["evidence"][0].pop("head")
            store = MagicMock()
            store.read.return_value = state
            store.notes_root = tmp_path / "notes"
            store.notes_root.mkdir()
            (store.notes_root / "verdict-round91-critic.md").write_text(
                f"## t — opencode\n\n**APPROVED** on {head[:12]}\n", encoding="utf-8")
            ok, reason = review_gate.check_pr_merge(store, tmp_path)
            self.assertFalse(ok)
            self.assertIn("no passing test evidence names the head being merged", reason)

    def test_check_pr_merge_refuses_a_pr_head_that_is_not_the_local_head(self) -> None:
        """Round 138: the merge ships the PR's head, not the local one.

        The gate looks at the local HEAD; `gh pr merge` ships the PR's head. If
        the two differ, the merge would ship a commit nobody checked here.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            self._repo_with_commit(tmp_path)
            store = MagicMock()
            store.read.return_value = {"phase": "verify"}
            ok, reason = review_gate.check_pr_merge(store, tmp_path, pr_head="deadbeef1234")
            self.assertFalse(ok)
            self.assertIn("is not the local HEAD", reason)

    def test_check_pr_merge_accepts_a_pr_head_equal_to_the_local_head(self) -> None:
        """The same commit spelled the same way passes: case and prefix are not
        two different commits."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            head = self._repo_with_commit(tmp_path)
            store = MagicMock()
            store.read.return_value = self._state(head)
            store.notes_root = tmp_path / "notes"
            store.notes_root.mkdir()
            (store.notes_root / "verdict-round91-critic.md").write_text(
                f"## t — opencode\n\n**APPROVED** on {head[:12]}\n", encoding="utf-8")
            ok, reason = review_gate.check_pr_merge(store, tmp_path, pr_head=head.upper())
            self.assertTrue(ok, reason)
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
                "evidence": [{"round": 60, "kind": "test", "ok": True, "head": head}],
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
                "evidence": [{"round": 60, "kind": "test", "ok": True, "head": head}],
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

            head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(tmp_path), capture_output=True, text=True).stdout.strip()

            store = MagicMock()
            store.read.return_value = {
                "phase": "verify",
                "round": 60,
                "writer": "agy",
                "evidence": [{"round": 60, "kind": "test", "ok": True, "head": head}],
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
                "evidence": [{"round": 60, "kind": "test", "ok": True, "head": head}],
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
                "evidence": [{"round": 60, "kind": "test", "ok": True, "head": head}],
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
                "evidence": [{"round": 60, "kind": "test", "ok": True, "head": head}],
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
                "evidence": [{"round": 60, "kind": "test", "ok": True, "head": head}],
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

    @patch("review_gate._preflight_merge", return_value=(True, ""))
    @patch("review_gate.run_gh_merge")
    def test_cmd_pr_force_with_reason_and_human_ok_records_audit(self, mock_gh: MagicMock,
                                                                  _mock_pf: MagicMock) -> None:
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

    @patch("review_gate.run_gh_merge")
    @patch("review_gate.resolve_pr_head", return_value=("", "gh not authenticated"))
    def test_cmd_pr_refuses_when_the_pr_head_cannot_be_read(self, _mock_head: MagicMock,
                                                             mock_merge: MagicMock) -> None:
        """Round 138: no answer about the PR's head, no merge.

        The gate checks the local HEAD; without the PR's head it cannot know
        whether the merge ships the commit it checked. Fail-closed beats a guess.
        """
        store = MagicMock()
        store.read.return_value = {"round": 91, "phase": "verify"}
        args = argparse.Namespace(pr_action="merge", force=False, reason="", pr_id="84", role="omp")
        res = review_gate.cmd_pr(store, args)
        self.assertEqual(res, 1)
        mock_merge.assert_not_called()

    @patch("review_gate.run_gh_merge")
    @patch("review_gate.resolve_pr_head", return_value=("deadbeef1234", ""))
    def test_cmd_pr_refuses_a_pr_head_the_gate_did_not_check(self, _mock_head: MagicMock,
                                                              mock_merge: MagicMock) -> None:
        """Round 138: the PR's head is not the local HEAD -> refuse, do not merge."""
        with tempfile.TemporaryDirectory() as tmp:
            store, base = self._store_with(tmp)
            head = self._repo_with_commit(base)
            state = self._state(head)
            store.mutate(lambda s: s.update(state))
            args = argparse.Namespace(pr_action="merge", force=False, reason="", pr_id="84", role="omp")
            res = review_gate.cmd_pr(store, args)
            self.assertEqual(res, 1)
            mock_merge.assert_not_called()

    @patch("review_gate.subprocess.run")
    def test_resolve_pr_head_reads_the_oid(self, mock_run: MagicMock) -> None:
        """The PR's head comes from GitHub, not from a guess."""
        mock_run.return_value = MagicMock(returncode=0, stdout="abc123def456\n", stderr="")
        head, err = review_gate.resolve_pr_head(Path("/tmp"), "84")
        self.assertEqual(head, "abc123def456")
        self.assertEqual(err, "")
        self.assertIn("headRefOid", " ".join(mock_run.call_args[0][0]))

    @patch("review_gate.subprocess.run")
    def test_resolve_pr_head_fails_closed_when_gh_fails(self, mock_run: MagicMock) -> None:
        """GitHub says nothing -> the gate is told nothing, and refuses."""
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="gh not authenticated")
        head, err = review_gate.resolve_pr_head(Path("/tmp"), "84")
        self.assertEqual(head, "")
        self.assertIn("gh not authenticated", err)

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
            "evidence": [{"round": 91, "kind": "test", "ok": True, "head": head}],
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

    def test_marker_in_first_block_decides(self) -> None:
        """Round 97 C1: newest MARKED block decides — marker in the first of
        three sections approves; unmarked tail never hides it."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            head = self._repo_with_commit(tmp_path)
            store = MagicMock()
            store.read.return_value = self._state(head)
            store.notes_root = tmp_path / "notes"
            store.notes_root.mkdir()
            (store.notes_root / "verdict-round91-critic.md").write_text(
                f"## t — opencode\n\nverdict-approve {head[:12]}\n"
                "\n## details\n\nprose, no marker\n"
                "\n## what this does NOT cover\n\nmore prose\n",
                encoding="utf-8")
            ok, reason = review_gate.check_pr_merge(store, tmp_path)
            self.assertTrue(ok, reason)

    def test_early_reject_beats_clean_tail(self) -> None:
        """Round 97 C1: NEEDS WORK in an early block refuses as rejected —
        not as no-marker — even with a clean tail."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            head = self._repo_with_commit(tmp_path)
            store = MagicMock()
            store.read.return_value = self._state(head)
            store.notes_root = tmp_path / "notes"
            store.notes_root.mkdir()
            (store.notes_root / "verdict-round91-critic.md").write_text(
                "## t — opencode\n\n**NEEDS WORK.** broken\n"
                "\n## details\n\nclean tail, no marker\n",
                encoding="utf-8")
            ok, reason = review_gate.check_pr_merge(store, tmp_path)
            self.assertFalse(ok)
            self.assertIn("rejected", reason)

    def test_newest_marker_pin_fail_no_fallback(self) -> None:
        """Round 97 C1: pin applies to the deciding block only — newest marker
        pins a stale sha while an older matches HEAD: refusal, no fallback."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            head = self._repo_with_commit(tmp_path)
            store = MagicMock()
            store.read.return_value = self._state(head)
            store.notes_root = tmp_path / "notes"
            store.notes_root.mkdir()
            (store.notes_root / "verdict-round91-critic.md").write_text(
                f"## old — opencode\n\nverdict-approve {head[:12]}\n"
                "\n## new — opencode\n\nverdict-approve deadbeef1234\n",
                encoding="utf-8")
            ok, reason = review_gate.check_pr_merge(store, tmp_path)
            self.assertFalse(ok)
            self.assertIn("does not match current HEAD", reason)


    def test_merge_record_writes_event_with_real_merge_commit(self) -> None:
        """The audit repair names GitHub's merge commit, not the writer's claim.

        Round 115 merged with a hand-run `gh pr merge`, so the merge happened
        and the transcript never learned of it. The repair reads the remote.
        """
        with tempfile.TemporaryDirectory() as tmp:
            store, _ = self._store_with(tmp)
            args = argparse.Namespace(
                pr_action="merge-record", pr_id="61", head="", base="master"
            )
            with patch.object(
                review_gate,
                "run_gh_view",
                return_value=(0, "MERGED 80fe82259cd9 2336d091870b"),
            ):
                code = review_gate.cmd_pr(store, args)
            self.assertEqual(code, 0)
            row = self._last_kind(store, "pr_merge_repaired")
            self.assertEqual(row["merge_commit"], "80fe82259cd9")
            self.assertEqual(row["head_oid"], "2336d091870b")
            self.assertEqual(row["base"], "master")

    def test_merge_record_refuses_head_that_github_did_not_merge(self) -> None:
        """A wrong --head must refuse: a trail repaired by hand is still a claim."""
        with tempfile.TemporaryDirectory() as tmp:
            store, _ = self._store_with(tmp)
            args = argparse.Namespace(
                pr_action="merge-record", pr_id="61", head="deadbeef", base="master"
            )
            with patch.object(
                review_gate,
                "run_gh_view",
                return_value=(0, "MERGED 80fe82259cd9 2336d091870b"),
            ):
                code = review_gate.cmd_pr(store, args)
            self.assertEqual(code, 1)
            self.assertIsNone(self._last_kind(store, "pr_merge_repaired"))

    def test_merge_record_refuses_open_pr(self) -> None:
        """An unmerged PR gets no repaired event — nothing was hidden."""
        with tempfile.TemporaryDirectory() as tmp:
            store, _ = self._store_with(tmp)
            args = argparse.Namespace(
                pr_action="merge-record", pr_id="62", head="", base="master"
            )
            with patch.object(
                review_gate,
                "run_gh_view",
                return_value=(0, "OPEN NONE 26a3acb2643e"),
            ):
                code = review_gate.cmd_pr(store, args)
            self.assertEqual(code, 1)
            self.assertIsNone(self._last_kind(store, "pr_merge_repaired"))

    def test_merge_record_refuses_when_gh_fails(self) -> None:
        """No remote answer, no repair: fail-closed beats a guessed event."""
        with tempfile.TemporaryDirectory() as tmp:
            store, _ = self._store_with(tmp)
            args = argparse.Namespace(
                pr_action="merge-record", pr_id="61", head="", base="master"
            )
            with patch.object(
                review_gate, "run_gh_view", return_value=(1, "gh not authenticated")
            ):
                code = review_gate.cmd_pr(store, args)
            self.assertEqual(code, 1)
            self.assertIsNone(self._last_kind(store, "pr_merge_repaired"))

    def test_merge_record_needs_a_pr_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, _ = self._store_with(tmp)
            args = argparse.Namespace(
                pr_action="merge-record", pr_id=None, head="", base="master"
            )
            self.assertEqual(review_gate.cmd_pr(store, args), 2)

    @staticmethod
    def _last_kind(store: Any, kind: str) -> dict[str, Any] | None:
        """The newest transcript row of `kind`, or None. Test-side reader."""
        import json

        path = store.project_root / ".pair" / "transcript.jsonl"
        if not path.exists():
            return None
        for line in path.read_text(encoding="utf-8").splitlines()[::-1]:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("kind") == kind:
                return row
        return None

    def _store_with(self, tmp: str) -> tuple[Any, Path]:
        """A store whose project_root is `tmp`, as `run_gh_view`'s cwd."""
        from store_core import Store

        base = Path(tmp)
        (base / ".pair").mkdir(parents=True, exist_ok=True)
        (base / ".pair" / "state.json").write_text("{}", encoding="utf-8")
        store = Store(base / ".pair", allow_create=True)
        return store, base


if __name__ == "__main__":
    unittest.main()
