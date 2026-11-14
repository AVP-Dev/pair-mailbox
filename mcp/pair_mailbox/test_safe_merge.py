"""Hermetic unit tests for scripts/safe-merge.sh.

Tests:
1. Clean 3-way merge succeeds and verifies changes.
2. Fast-forward merge with --ff-only succeeds.
3. Conflict preflight detects conflicts via git merge-tree without modifying branches or index.
4. Post-merge verification failure triggers clean rollback to pre-merge TARGET_ORIG_SHA,
   even when executed from a feature branch, and returns to initial branch.
5. Dirty working tree is refused before attempting merge.
6. Missing branches or invalid arguments exit with appropriate error codes.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SAFE_MERGE_SCRIPT = REPO_ROOT / "scripts" / "safe-merge.sh"


class TestSafeMerge(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        # Initialize hermetic git repo
        subprocess.run(["git", "init", "-b", "main"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.repo, check=True)

        # Initial commit
        (self.repo / "base.txt").write_text("initial base content\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=self.repo, check=True, capture_output=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_safe_merge(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(SAFE_MERGE_SCRIPT)] + list(args),
            cwd=self.repo,
            capture_output=True,
            text=True,
        )

    def test_clean_3way_merge(self) -> None:
        # Create target branch with commit
        (self.repo / "target_file.txt").write_text("target content\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-m", "target commit"], cwd=self.repo, check=True, capture_output=True)

        # Create feature branch branched from root commit
        subprocess.run(["git", "checkout", "-b", "feature", "HEAD~1"], cwd=self.repo, check=True, capture_output=True)
        (self.repo / "feature_file.txt").write_text("feature content\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-m", "feature commit"], cwd=self.repo, check=True, capture_output=True)

        # Run safe-merge main feature from feature branch
        res = self.run_safe_merge("main", "feature")
        self.assertEqual(res.returncode, 0, f"safe-merge failed: {res.stderr}\n{res.stdout}")
        self.assertIn("Integration complete!", res.stdout)

        # Verify current branch is restored to feature
        current = subprocess.check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=self.repo, text=True).strip()
        self.assertEqual(current, "feature")

        # Verify main has both files
        subprocess.run(["git", "checkout", "main"], cwd=self.repo, check=True, capture_output=True)
        self.assertTrue((self.repo / "target_file.txt").exists())
        self.assertTrue((self.repo / "feature_file.txt").exists())

    def test_ff_only_merge(self) -> None:
        # Feature branch directly ahead of main
        subprocess.run(["git", "checkout", "-b", "feature"], cwd=self.repo, check=True, capture_output=True)
        (self.repo / "feature_ff.txt").write_text("ff content\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-m", "ff commit"], cwd=self.repo, check=True, capture_output=True)
        feat_sha = subprocess.check_output(["git", "rev-parse", "feature"], cwd=self.repo, text=True).strip()

        # Switch back to main and merge with --ff-only
        subprocess.run(["git", "checkout", "main"], cwd=self.repo, check=True, capture_output=True)
        res = self.run_safe_merge("main", "feature", "--ff-only")
        self.assertEqual(res.returncode, 0, f"ff merge failed: {res.stderr}\n{res.stdout}")

        main_sha = subprocess.check_output(["git", "rev-parse", "main"], cwd=self.repo, text=True).strip()
        self.assertEqual(main_sha, feat_sha)

    def test_conflict_detected_and_branches_preserved(self) -> None:
        # Edit base.txt on main
        (self.repo / "base.txt").write_text("main conflict content\n", encoding="utf-8")
        subprocess.run(["git", "commit", "-am", "main conflict"], cwd=self.repo, check=True, capture_output=True)
        main_sha_before = subprocess.check_output(["git", "rev-parse", "main"], cwd=self.repo, text=True).strip()

        # Edit base.txt differently on feature branch
        subprocess.run(["git", "checkout", "-b", "feature", "HEAD~1"], cwd=self.repo, check=True, capture_output=True)
        (self.repo / "base.txt").write_text("feature conflict content\n", encoding="utf-8")
        subprocess.run(["git", "commit", "-am", "feature conflict"], cwd=self.repo, check=True, capture_output=True)
        feat_sha_before = subprocess.check_output(["git", "rev-parse", "feature"], cwd=self.repo, text=True).strip()

        res = self.run_safe_merge("main", "feature")
        self.assertEqual(res.returncode, 1)
        self.assertIn("CONFLICT", res.stderr)

        # Both branches must remain unchanged
        main_sha_after = subprocess.check_output(["git", "rev-parse", "main"], cwd=self.repo, text=True).strip()
        feat_sha_after = subprocess.check_output(["git", "rev-parse", "feature"], cwd=self.repo, text=True).strip()
        self.assertEqual(main_sha_before, main_sha_after)
        self.assertEqual(feat_sha_before, feat_sha_after)

        # Working tree must remain clean
        status = subprocess.check_output(["git", "status", "--porcelain"], cwd=self.repo, text=True).strip()
        self.assertEqual(status, "")

    def test_verify_failure_rolls_back_target(self) -> None:
        # Create commit on main
        (self.repo / "main_data.txt").write_text("main data\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-m", "main data"], cwd=self.repo, check=True, capture_output=True)
        main_sha_orig = subprocess.check_output(["git", "rev-parse", "main"], cwd=self.repo, text=True).strip()

        # Create commit on feature
        subprocess.run(["git", "checkout", "-b", "feature", "HEAD~1"], cwd=self.repo, check=True, capture_output=True)
        (self.repo / "feature_data.txt").write_text("feature data\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-m", "feature data"], cwd=self.repo, check=True, capture_output=True)

        # Install mock verify-changed.sh that always FAILS
        scripts_dir = self.repo / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        failing_verify = scripts_dir / "verify-changed.sh"
        failing_verify.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        failing_verify.chmod(0o755)
        # Commit verify script on feature branch so working tree is clean
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-m", "add verify script"], cwd=self.repo, check=True, capture_output=True)

        res = self.run_safe_merge("main", "feature")
        self.assertEqual(res.returncode, 1)
        self.assertIn("Post-merge verification checks failed", res.stderr)
        self.assertIn("Rollback complete", res.stderr)

        # Verify main is rolled back strictly to main_sha_orig
        main_sha_after = subprocess.check_output(["git", "rev-parse", "main"], cwd=self.repo, text=True).strip()
        self.assertEqual(main_sha_orig, main_sha_after)

        # Current branch restored to feature
        current = subprocess.check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=self.repo, text=True).strip()
        self.assertEqual(current, "feature")

    def test_dirty_working_tree_refuses(self) -> None:
        (self.repo / "base.txt").write_text("uncommitted change\n", encoding="utf-8")
        res = self.run_safe_merge("main", "main")
        self.assertEqual(res.returncode, 1)
        self.assertIn("Working directory has uncommitted tracked changes", res.stderr)

    def test_invalid_arguments_refuse(self) -> None:
        res = self.run_safe_merge()
        self.assertEqual(res.returncode, 2)

        res = self.run_safe_merge("nonexistent_branch_a", "nonexistent_branch_b")
        self.assertEqual(res.returncode, 1)
        self.assertIn("Target branch 'nonexistent_branch_a' does not exist", res.stderr)


if __name__ == "__main__":
    unittest.main()
