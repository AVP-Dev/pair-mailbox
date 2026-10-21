"""Tests for scripts/merge-audit-trail.py.

Behavioural: a real journal (the active file and a rotated archive) in a
temporary tree, a real `gh` executable on the PATH, the real script run over
them. The rules this check exists for are exactly the ones a mocked `gh` could
not see: a merge that escaped the sanctioned path, a `gh` that cannot answer,
and a journal whose rotated archive holds the older events.

Same shape as scripts/backlog_check_test.py: no network, local files.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "merge-audit-trail.py"


def write_gh(directory: Path, body: str) -> None:
    """A real `gh` on the PATH: the script runs it like any other command."""
    path = directory / "gh"
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def event(pr_id: int) -> str:
    return json.dumps({"kind": "pr_merged", "pr_id": pr_id, "role": "writer"})


class MergeAuditTrailTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.journal = self.root / ".pair"
        (self.journal / "transcript-archive").mkdir(parents=True)
        self.baseline = self.root / "baseline.tsv"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.addCleanup(self._tmp.cleanup)

    def run_audit(self, *extra: str, gh: str | None = None,
                  path: str | None = None) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        if path is not None:
            env["PATH"] = path
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--repo", str(self.root),
             "--journal", str(self.journal), "--baseline", str(self.baseline),
             *extra],
            capture_output=True, text=True, timeout=60, env=env,
        )

    def test_a_merged_pr_with_an_event_is_not_an_escape(self) -> None:
        """The ordinary case: the merge went through the sanctioned path."""
        (self.journal / "transcript.jsonl").write_text(event(7) + "\n", encoding="utf-8")
        self.baseline.write_text("", encoding="utf-8")
        write_gh(self.bin, "#!/bin/sh\necho '[{\"number\": 7}]'\n")
        proc = self.run_audit(path=f"{self.bin}:{os.environ['PATH']}")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("PASS", proc.stdout)
        self.assertIn("1 merged PRs", proc.stdout)

    def test_a_merge_without_an_event_fails_and_is_named(self) -> None:
        """A merge outside the sanctioned path is the thing this check exists for."""
        (self.journal / "transcript.jsonl").write_text(event(7) + "\n", encoding="utf-8")
        self.baseline.write_text("", encoding="utf-8")
        write_gh(self.bin, "#!/bin/sh\necho '[{\"number\": 7}, {\"number\": 9}]'\n")
        proc = self.run_audit(path=f"{self.bin}:{os.environ['PATH']}")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("9", proc.stdout)
        self.assertIn("escaped the sanctioned path", proc.stdout)

    def test_the_known_debt_is_named_and_passes(self) -> None:
        """The debt the baseline names is reported as debt, not as a failure."""
        (self.journal / "transcript.jsonl").write_text(event(7) + "\n", encoding="utf-8")
        self.baseline.write_text("9\tmerged before the gate existed\n", encoding="utf-8")
        write_gh(self.bin, "#!/bin/sh\necho '[{\"number\": 7}, {\"number\": 9}]'\n")
        proc = self.run_audit(path=f"{self.bin}:{os.environ['PATH']}")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("known debt: 1", proc.stdout)

    def test_an_event_in_a_rotated_archive_counts(self) -> None:
        """The rotation moved the older events; the check must still see them."""
        (self.journal / "transcript.jsonl").write_text(event(7) + "\n", encoding="utf-8")
        (self.journal / "transcript-archive" / "2.jsonl").write_text(
            event(9) + "\n", encoding="utf-8")
        self.baseline.write_text("", encoding="utf-8")
        write_gh(self.bin, "#!/bin/sh\necho '[{\"number\": 7}, {\"number\": 9}]'\n")
        proc = self.run_audit(path=f"{self.bin}:{os.environ['PATH']}")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("2 merged PRs, 2 with a merge record", proc.stdout)

    def test_a_repaired_or_forced_merge_counts_as_recorded(self) -> None:
        """The journal records a merge in three ways; all of them are records."""
        (self.journal / "transcript.jsonl").write_text(
            event(7) + "\n" + json.dumps({"kind": "pr_merge_repaired", "pr_id": 8}) + "\n"
            + json.dumps({"kind": "pr_merge_forced", "pr_id": 9}) + "\n",
            encoding="utf-8")
        self.baseline.write_text("", encoding="utf-8")
        write_gh(self.bin, "#!/bin/sh\necho '[{\"number\": 7}, {\"number\": 8}, "
                           "{\"number\": 9}]'\n")
        proc = self.run_audit(path=f"{self.bin}:{os.environ['PATH']}")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("3 merged PRs, 3 with a merge record", proc.stdout)

    def test_a_repaid_baseline_entry_is_a_warning(self) -> None:
        """The debt shrank: the baseline must be updated, and that is said out loud."""
        (self.journal / "transcript.jsonl").write_text(event(7) + "\n", encoding="utf-8")
        self.baseline.write_text("7\tthe debt, now repaid\n", encoding="utf-8")
        write_gh(self.bin, "#!/bin/sh\necho '[{\"number\": 7}]'\n")
        proc = self.run_audit(path=f"{self.bin}:{os.environ['PATH']}")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("WARNING", proc.stdout)
        self.assertIn("update the baseline", proc.stdout)

    def test_without_gh_the_check_fails_closed(self) -> None:
        """No gh means no answer about the merged PRs - that is a failure, not a pass."""
        (self.journal / "transcript.jsonl").write_text(event(7) + "\n", encoding="utf-8")
        self.baseline.write_text("", encoding="utf-8")
        empty = self.root / "empty-bin"
        empty.mkdir()
        proc = self.run_audit(path=str(empty))
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("FAILED", proc.stdout)
        self.assertNotIn("PASS", proc.stdout)

    def test_a_gh_that_cannot_answer_fails_closed(self) -> None:
        """An unauthenticated gh is the same blindness, and it fails the same way."""
        (self.journal / "transcript.jsonl").write_text(event(7) + "\n", encoding="utf-8")
        self.baseline.write_text("", encoding="utf-8")
        write_gh(self.bin, "#!/bin/sh\necho 'gh: not authenticated' >&2\nexit 1\n")
        proc = self.run_audit(path=f"{self.bin}:{os.environ['PATH']}")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("not authenticated", proc.stdout)

    def test_without_a_journal_the_check_fails_closed(self) -> None:
        """The journal is the pair's state, not the repo's: without it there is nothing to audit."""
        self.baseline.write_text("", encoding="utf-8")
        write_gh(self.bin, "#!/bin/sh\necho '[{\"number\": 7}]'\n")
        self.journal = self.root / "no-state-dir"
        proc = self.run_audit(path=f"{self.bin}:{os.environ['PATH']}")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("no journal", proc.stdout)

    def test_an_empty_journal_fails_closed(self) -> None:
        """A state dir with no transcript holds no events: every merge would be an escape."""
        self.baseline.write_text("", encoding="utf-8")
        write_gh(self.bin, "#!/bin/sh\necho '[{\"number\": 7}]'\n")
        proc = self.run_audit(path=f"{self.bin}:{os.environ['PATH']}")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("no transcript", proc.stdout)

    def test_a_missing_baseline_fails_closed(self) -> None:
        """The debt must be named: a check that cannot say what is known cannot pass."""
        (self.journal / "transcript.jsonl").write_text(event(7) + "\n", encoding="utf-8")
        write_gh(self.bin, "#!/bin/sh\necho '[{\"number\": 7}]'\n")
        proc = self.run_audit(path=f"{self.bin}:{os.environ['PATH']}")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("no baseline", proc.stdout)


if __name__ == "__main__":
    unittest.main()
