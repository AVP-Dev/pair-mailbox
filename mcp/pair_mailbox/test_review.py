#!/usr/bin/env python3
"""Binding verdicts to revisions plus a queue limit (review-queue-and-stale-verdicts).

A verdict covers a code state at review time, not the task at large: tracked files, the
queue caps at 2, done blocks over pending/stale.
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
        """The same file rewritten over — the verdict goes stale."""
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
        self.assertTrue(stale, f"the same file moved, stale must follow: {why}")

    def test_fresh_when_unrelated_files_changed(self) -> None:
        """The writer moved to another module — the verdict stays fresh (dirty overlap)."""
        import review
        old = review.snapshot(self.repo)
        (self.repo / "b.py").write_text("y=1\n", encoding="utf-8")
        git(self.repo, "add", "b.py")
        git(self.repo, "commit", "-m", "add b")
        current = review.git_head(self.repo)
        stale, _ = review.is_stale(old, current,
                                   review.changed_between(self.repo, old["head"], current),
                                   review.dirty_files(self.repo))
        self.assertFalse(stale, "foreign files never invalidate the verdict")

    def test_fail_open_without_git(self) -> None:
        import review
        with tempfile.TemporaryDirectory() as tmp:
            plain = Path(tmp)
            snap = review.snapshot(plain)
            self.assertEqual("", snap["head"])
            stale, _ = review.is_stale({"head": "abc", "files": ["a.py"]}, "", [], [])
            self.assertFalse(stale, "with no git the round must never hold")

    def test_dirty_files_filters_untracked_by_default(self) -> None:
        """U1: dirty_files skips untracked (??) files by default."""
        import review
        (self.repo / "untracked_card.md").write_text("backlog card\n", encoding="utf-8")
        self.assertEqual([], review.dirty_files(self.repo))
        self.assertEqual(["untracked_card.md"], review.dirty_files(self.repo, include_untracked=True))

    def test_snapshot_includes_head_files_with_untracked_scratch(self) -> None:
        """U1: snapshot() unions head_files ∪ tracked-dirty, never pushing commit files out with scratch."""
        import review
        (self.repo / "scratch.txt").write_text("scratch\n", encoding="utf-8")
        snap = review.snapshot(self.repo)
        self.assertIn("a.py", snap["files"], "commit files must stand in the snapshot coverage")
        self.assertNotIn("scratch.txt", snap["files"], "untracked scratch must never pollute the snapshot")

    def test_is_stale_untracked_scratch_does_not_invalidate(self) -> None:
        "U2(a): an untracked scratch file appearing past the verdict never stales it."
        import review
        old = review.snapshot(self.repo)
        current = review.git_head(self.repo)
        (self.repo / "scratch.txt").write_text("new untracked\n", encoding="utf-8")
        stale, reason = review.is_stale(
            old, current,
            review.changed_between(self.repo, old["head"], current),
            review.dirty_files(self.repo),
        )
        self.assertFalse(stale, f"an untracked file must never cause eternal stale: {reason}")

    def test_is_stale_tracked_edit_invalidates(self) -> None:
        "U2(b): a real tracked edit of the reviewed file stales the verdict."
        import review
        old = review.snapshot(self.repo)
        current = review.git_head(self.repo)
        (self.repo / "a.py").write_text("x=999\n", encoding="utf-8")
        stale, reason = review.is_stale(
            old, current,
            review.changed_between(self.repo, old["head"], current),
            review.dirty_files(self.repo),
        )
        self.assertTrue(stale, "a tracked edit must stale the verdict")
        self.assertIn("a.py", reason)

    def test_is_stale_tracked_edit_unrelated_does_not_invalidate(self) -> None:
        "U2(c): a tracked edit of an unrelated file never stales the verdict."
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
        self.assertFalse(stale, "a tracked edit of a foreign file must never invalidate the verdict")


class TestDeliveryFloor(unittest.TestCase):
    """Round 91 floor: HEAD visible from a local remote-tracking ref.

    Local git only — A8: the predicate must never invoke ls-remote, fetch, or
    gh. C3: the round-88 shape (N commits ahead, no remote branch) blocks; the
    numbers reproduce the real failure (94 ahead) rather than an invented N=1.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        # A bare "origin" plus a clone: pushes update refs/remotes/* locally,
        # no network, exactly the writer-pushes case the floor must see.
        self.origin = self.repo / "origin.git"
        self.origin.mkdir()
        git(self.origin, "init", "--bare", "-q")
        self.work = self.repo / "work"
        git(self.repo, "clone", "-q", str(self.origin), str(self.work))
        git(self.work, "config", "user.email", "t@t.t")
        git(self.work, "config", "user.name", "t")
        (self.work / "a.py").write_text("x=1\n", encoding="utf-8")
        git(self.work, "add", "a.py")
        git(self.work, "commit", "-qm", "init")
        git(self.work, "push", "-q", "origin", "master")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_pushed_head_is_delivered_a1(self) -> None:
        import review
        status, _ = review.delivery_floor(self.work)
        self.assertEqual(status, "delivered")

    def test_ahead_by_94_with_no_remote_branch_blocks_c3(self) -> None:
        """Round 88's shape: 94 commits, no remote branch carrying HEAD."""
        import review
        for i in range(94):
            (self.work / "a.py").write_text(f"x={i}\n", encoding="utf-8")
            git(self.work, "add", "a.py")
            git(self.work, "commit", "-qm", f"c{i}")
        status, detail = review.delivery_floor(self.work)
        self.assertEqual(status, "undelivered", detail)
        self.assertIn("git push", detail)

    def test_pushed_feature_branch_resolves_a2(self) -> None:
        """Round-89 shape: pushed, unmerged — the floor must never block."""
        import review
        git(self.work, "checkout", "-qb", "feature")
        (self.work / "a.py").write_text("x=2\n", encoding="utf-8")
        git(self.work, "add", "a.py")
        git(self.work, "commit", "-qm", "feat")
        git(self.work, "push", "-q", "origin", "feature")
        status, _ = review.delivery_floor(self.work)
        self.assertEqual(status, "delivered")

    def test_no_remote_is_unknown_not_undelivered_a4(self) -> None:
        """Cannot-check is loud, never a hard block, never scored as a pass."""
        import review
        git(self.work, "remote", "remove", "origin")
        status, _ = review.delivery_floor(self.work)
        self.assertEqual(status, "unknown")

    def test_never_fetched_remote_is_unknown_a5(self) -> None:
        """Zero refs/remotes/* must not read as 'nothing was pushed'."""
        import review
        fresh = self.repo / "fresh"
        git(self.repo, "clone", "-q", "--no-checkout", str(self.origin), str(fresh))
        proc = subprocess.run(
            ["git", "for-each-ref", "--format=%(refname)", "refs/remotes/"],
            cwd=str(fresh), capture_output=True, text=True, timeout=10)
        # Clone normally fetches; deleting the refs simulates never-fetched.
        for line in proc.stdout.splitlines():
            if line.strip():
                git(fresh, "update-ref", "-d", line.strip())
        status, detail = review.delivery_floor(fresh)
        self.assertEqual(status, "unknown", detail)
        self.assertNotIn("push first", detail)

    def test_not_a_repo_is_unknown_a6(self) -> None:
        import review
        with tempfile.TemporaryDirectory() as tmp:
            status, _ = review.delivery_floor(Path(tmp))
            self.assertEqual(status, "unknown")

    def test_no_network_command_on_the_floor_path_a8(self) -> None:
        """The floor path must never invoke ls-remote, fetch, or gh."""
        import review
        seen: list[list[str]] = []
        real_run = subprocess.run

        def spy(*args: object, **kwargs: object) -> object:
            cmd = list(args[0]) if args else []
            seen.append([str(c) for c in cmd])
            return real_run(*args, **kwargs)

        with mock.patch.object(review.subprocess, "run", spy):
            review.delivery_floor(self.work)
        joined = " ".join(" ".join(c) for c in seen)
        self.assertNotIn("ls-remote", joined)
        self.assertNotIn("fetch", joined)
        for cmd in seen:
            self.assertNotEqual(cmd[:1], ["gh"])


class TestPrCeiling(unittest.TestCase):
    """Round 91 ceiling: verified PR state per autonomy, advance-path only."""

    def test_semi_open_resolves_b1(self) -> None:
        import review
        head = "a" * 40
        opens = [{"number": 7, "headRefOid": head, "headRefName": "feat"}]
        with mock.patch.object(review, "git_head", return_value=head), \
                mock.patch("subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = __import__("json").dumps(opens)
            status, _ = review.pr_ceiling(Path("."), "semi")
        self.assertEqual(status, "resolved")

    def test_broken_probe_is_unknown_not_undelivered(self) -> None:
        """Finding 1: a merge-base timeout/lock/damage after good rev-parse
        must read cannot-check, never \"you never pushed\"."""
        import review
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            git(repo, "init", "-q")
            git(repo, "config", "user.email", "t@t.t")
            git(repo, "config", "user.name", "t")
            (repo / "a.py").write_text("x=1\n", encoding="utf-8")
            git(repo, "add", "a.py")
            git(repo, "commit", "-qm", "init")
            git(repo, "remote", "add", "origin", "https://example.com/o/r.git")
            git(repo, "update-ref", "refs/remotes/origin/master", "HEAD")
            with mock.patch.object(review.subprocess, "run",
                                   side_effect=OSError("locked")):
                status, detail = review.delivery_floor(repo)
            # git_head itself also breaks under the mock, so the floor reports
            # unknown either way — but never undelivered, which would block
            # with a push command on a repo it could not even read.
            self.assertEqual(status, "unknown", detail)
            self.assertNotIn("push first", detail)


    def test_semi_merged_resolves_b2(self) -> None:
        """Merged is stronger than open, not a failure."""
        import review
        head = "b" * 40
        assoc = [{"number": 8, "merged_at": "2026-10-14T08:46:22Z"}]
        with mock.patch.object(review, "git_head", return_value=head), \
                mock.patch.object(review, "_repo_slug", return_value="o/r"), \
                mock.patch("subprocess.run") as run:
            def side_effect(cmd: object, **kwargs: object) -> object:
                out = mock.Mock()
                out.returncode = 0
                args = [str(a) for a in list(cmd)]  # type: ignore[arg-type]
                if "api" in args:
                    out.stdout = __import__("json").dumps(assoc)
                else:
                    out.stdout = "[]"
                return out
            run.side_effect = side_effect
            status, _ = review.pr_ceiling(Path("."), "semi")
        self.assertEqual(status, "resolved")

    def test_semi_no_pr_blocks_b3(self) -> None:
        import review
        with mock.patch.object(review, "git_head", return_value="c" * 40), \
                mock.patch("subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = "[]"
            status, detail = review.pr_ceiling(Path("."), "semi")
        self.assertEqual(status, "missing")
        self.assertIn("gh pr create", detail)

    def test_auto_merged_resolves_b4(self) -> None:
        import review
        head = "d" * 40
        assoc = [{"number": 9, "merged_at": "2026-10-14T08:46:22Z"}]
        with mock.patch.object(review, "git_head", return_value=head), \
                mock.patch.object(review, "_repo_slug", return_value="o/r"), \
                mock.patch("subprocess.run") as run:
            def side_effect(cmd: object, **kwargs: object) -> object:
                out = mock.Mock()
                out.returncode = 0
                args = [str(a) for a in list(cmd)]  # type: ignore[arg-type]
                if "api" in args:
                    out.stdout = __import__("json").dumps(assoc)
                else:
                    out.stdout = "[]"
                return out
            run.side_effect = side_effect
            status, _ = review.pr_ceiling(Path("."), "auto")
        self.assertEqual(status, "resolved")

    def test_auto_open_unmerged_blocks_b5(self) -> None:
        import review
        head = "e" * 40
        opens = [{"number": 10, "headRefOid": head, "headRefName": "feat"}]
        with mock.patch.object(review, "git_head", return_value=head), \
                mock.patch("subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = __import__("json").dumps(opens)
            status, detail = review.pr_ceiling(Path("."), "auto")
        self.assertEqual(status, "missing")
        self.assertIn("end to end", detail)

    def test_gh_missing_is_unknown_b8(self) -> None:
        import review
        with mock.patch.object(review, "git_head", return_value="f" * 40), \
                mock.patch("subprocess.run", side_effect=OSError("no gh")):
            status, detail = review.pr_ceiling(Path("."), "auto")
        self.assertEqual(status, "unknown")
        self.assertIn("not scored as a pass", detail)

class TestReviewServer(PairTestCase):
    def fixed_snapshot(self, head: str = "h" * 40, files: list | None = None):
        return {"head": head, "dirty": "d123", "files": files if files is not None else ["a.py"]}

    def test_obligation_stores_rev(self) -> None:
        import review
        self.writer.tool_pair_state()
        self.peer.tool_pair_state()
        with mock.patch.object(review, "snapshot", return_value=self.fixed_snapshot()):
            self.writer.tool_pair_send(to="reviewer-agent", text="asking for a review",
                                       expects_reply=True)
        obligations = list(self.store.obligations_root.glob("*.json"))
        self.assertEqual(1, len(obligations))
        record = json.loads(obligations[0].read_text(encoding="utf-8"))
        self.assertIn("rev", record, "a request must pin the revision")
        self.assertEqual("h" * 40, record["rev"].get("head"))

    def test_queue_limit_refuses_third(self) -> None:
        "A third unacked request refuses, pointing at pair_read."
        import review
        self.writer.tool_pair_state()
        self.peer.tool_pair_state()
        with mock.patch.object(review, "snapshot", return_value=self.fixed_snapshot()):
            self.writer.tool_pair_send(to="reviewer-agent", text="review 1", expects_reply=True)
            self.writer.tool_pair_send(to="reviewer-agent", text="review 2", expects_reply=True)
            with self.assertRaises(Exception) as caught:
                self.writer.tool_pair_send(to="reviewer-agent", text="review 3",
                                           expects_reply=True)
        self.assertIn("queue", str(caught.exception).lower())

    def test_pending_reviews_block_done(self) -> None:
        import review
        self.writer.tool_pair_state()
        self.peer.tool_pair_state()
        with mock.patch.object(review, "snapshot", return_value=self.fixed_snapshot()):
            self.writer.tool_pair_send(to="reviewer-agent", text="asking for a review",
                                       expects_reply=True)
        blockers = self.writer._done_blockers(self.store.read())
        self.assertTrue(any("pending review" in b for b in blockers),
                        f"an open queue must hold done: {blockers}")

    def test_stale_verdict_blocks_done(self) -> None:
        "A new commit over the verdict on the same files blocks done."
        import review
        self.writer.tool_pair_state()
        self.peer.tool_pair_state()
        old = self.fixed_snapshot(head="a" * 40, files=["a.py"])
        with mock.patch.object(review, "snapshot", return_value=old):
            self.peer.tool_pair_note(kind="verdict", text="checked: ok")
        with mock.patch.object(review, "git_head", return_value="b" * 40), \
                mock.patch.object(review, "changed_between", return_value=["a.py"]), \
                mock.patch.object(review, "dirty_files", return_value=[]):
            blockers = self.writer._done_blockers(self.store.read())
        self.assertTrue(any("stale" in b for b in blockers),
                        f"a stale verdict must hold done: {blockers}")

    def test_fresh_verdict_does_not_block_done(self) -> None:
        import review
        self.writer.tool_pair_state()
        self.peer.tool_pair_state()
        snap = self.fixed_snapshot(head="a" * 40, files=["a.py"])
        with mock.patch.object(review, "snapshot", return_value=snap):
            self.peer.tool_pair_note(kind="verdict", text="checked: ok")
        with mock.patch.object(review, "git_head", return_value="a" * 40), \
                mock.patch.object(review, "changed_between", return_value=[]), \
                mock.patch.object(review, "dirty_files", return_value=[]):
            blockers = [b for b in self.writer._done_blockers(self.store.read())
                        if "stale" in b or "pending review" in b]
        self.assertEqual([], blockers, f"a fresh verdict must never hold: {blockers}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
