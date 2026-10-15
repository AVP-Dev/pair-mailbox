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
        # CI 3.10 teardown race: a background git gc holds .git/objects/pack
        # open while rmtree walks it (Errno 39). Retry, then let go — a temp
        # dir leftover never fails a test.
        import errno as _errno
        import shutil as _shutil
        import time as _time
        for _ in range(5):
            try:
                self._tmp.cleanup()
                break
            except OSError as exc:
                if exc.errno != _errno.ENOTEMPTY:
                    raise
                _time.sleep(0.2)
        else:
            _shutil.rmtree(self._tmp.name, ignore_errors=True)

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
        # CI 3.10 teardown race: a background git gc holds .git/objects/pack
        # open while rmtree walks it (Errno 39). Retry, then let go — a temp
        # dir leftover never fails a test.
        import errno as _errno
        import shutil as _shutil
        import time as _time
        for _ in range(5):
            try:
                self._tmp.cleanup()
                break
            except OSError as exc:
                if exc.errno != _errno.ENOTEMPTY:
                    raise
                _time.sleep(0.2)
        else:
            _shutil.rmtree(self._tmp.name, ignore_errors=True)

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

class TestDeliveryGaps(PairTestCase):
    """Task #100: pin what round 91 proved in code but left untested.

    Hardening, not behaviour: each test fails only if the guarded line rots.
    """

    def test_pr_row_string_never_satisfies_ceiling_e1(self) -> None:
        """E1: a kind=pr evidence row is a string a role wrote — the ceiling
        must verify against gh, never trust the row (server.py:446-455)."""
        import review
        head = "1" * 40
        self.writer.tool_pair_claim(reason="writer")
        self.advance_phases()
        self.record_test_evidence()
        self.writer.tool_pair_evidence(
            kind="pr", value="https://github.com/x/y/pull/999", ok=True)
        with mock.patch.object(review, "git_head", return_value=head), \
                mock.patch("subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = "[]"
            status, _ = review.pr_ceiling(Path("."), "semi")
        self.assertEqual(status, "missing",
                         "a pr row must never satisfy the ceiling: gh says no PR")

    def test_unreachable_commit_row_blocks_e2(self) -> None:
        """E2: a kind=commit row naming an oid no remote reaches still blocks.

        A bogus oid fails merge-base with exit 128 (not exit 1), so the probe
        answers None (cannot-check): the floor maps it to unknown, which never
        closes as delivered either way. Pinned as never-True, not as False.
        """
        import review
        self.assertIsNone(review.head_reachable_from(
            Path("."), "refs/remotes/origin/master",
            "deadbeef" * 5))

    def test_missing_pr_row_blocks_e3(self) -> None:
        """E3: a ceiling check for a PR gh never heard of blocks, loudly."""
        import review
        with mock.patch.object(review, "git_head", return_value="2" * 40), \
                mock.patch("subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = "[]"
            status, detail = review.pr_ceiling(Path("."), "auto")
        self.assertEqual(status, "missing")
        self.assertIn("merge first", detail)

    def test_audit_exempt_from_ceiling_b6(self) -> None:
        """B6: mode=audit never reaches the ceiling — findings, not a diff.

        Both directions, or Mutation 2 (floor append removed) passes it
        vacuously: audit shows no delivery/ship blocker AND fix on the same
        undelivered tree does. One direction alone distinguishes nothing.
        """
        import server as _server
        git(Path(self._tmp.name), "init", "-q", "--bare", "b6-origin.git")
        git(Path(self._tmp.name), "clone", "-q",
            str(Path(self._tmp.name) / "b6-origin.git"), "b6")
        work = Path(self._tmp.name) / "b6"
        git(work, "config", "user.email", "t@t.t")
        git(work, "config", "user.name", "t")
        (work / "f.txt").write_text("x\n", encoding="utf-8")
        git(work, "add", "f.txt")
        git(work, "commit", "-qm", "init")
        git(work, "push", "-q", "origin", "master")
        (work / "f.txt").write_text("y\n", encoding="utf-8")
        git(work, "add", "f.txt")
        git(work, "commit", "-qm", "advance")
        for mode, wants_blocker in (("audit", False), ("fix", True)):
            store = _server.Store(work / f".pair-{mode}")
            writer = _server.PairSession("writer-agent", store)
            store.mutate(lambda s, m=mode: (
                s.__setitem__("writer", "writer-agent"),
                s.__setitem__("mode", m)))
            blockers = [b for b in writer._done_blockers(store.read())
                        if "shipped" in b or "not delivered" in b]
            if wants_blocker:
                self.assertTrue(any("not delivered" in b for b in blockers),
                                f"fix must carry the floor blocker: {blockers}")
            else:
                self.assertEqual(blockers, [],
                                 f"audit must carry no delivery blocker: {blockers}")

    def test_state_carries_advisory_not_ceiling_b7(self) -> None:
        """B7: pair_state carries the advisory line, never the ceiling verdict."""
        state = self.writer.tool_pair_state()
        self.assertIn("delivery", state)
        self.assertIn("floor", state["delivery"])
        self.assertIn("ceiling", state["delivery"])

    def test_push_then_state_never_stale_a7(self) -> None:
        """A7 sequence: a real push between two pair_state calls moves the
        answer — the stale \"not delivered\" blocker must not survive behind
        unchanged:true.

        Full sequence, not a mechanism probe: the session is re-homed into a
        real repo with a bare local origin (no daemon; push updates
        refs/remotes/* as part of itself, which is what the floor reads).
        Pre-push the floor blocks; post-push the same pair_state must answer
        full, never the stub. Mutation-proof: with needs_delivery removed
        from statecache's gate both calls fingerprint identically and the
        second answers unchanged:true — failing here, not green. (The
        mechanism half — snapshot fires in fix without verdicts — is pinned
        separately by test_snapshot_runs_without_verdicts_in_fix.)
        """
        import os
        import statecache
        import server as _server
        git(Path(self._tmp.name), "init", "-q", "--bare", "origin.git")
        git(Path(self._tmp.name), "clone", "-q",
            str(Path(self._tmp.name) / "origin.git"), "work")
        work = Path(self._tmp.name) / "work"
        git(work, "config", "user.email", "t@t.t")
        git(work, "config", "user.name", "t")
        (work / "f.txt").write_text("x\n", encoding="utf-8")
        git(work, "add", "f.txt")
        git(work, "commit", "-qm", "init")
        git(work, "push", "-q", "origin", "master")
        (work / "f.txt").write_text("y\n", encoding="utf-8")
        git(work, "add", "f.txt")
        git(work, "commit", "-qm", "advance")
        # Re-home: Store(root) treats root as the project and root/.pair as
        # state (project_root property) — so the floor reads this repo.
        store = _server.Store(work / ".pair")
        writer = _server.PairSession("writer-agent", store)
        store.mutate(lambda s: s.__setitem__("writer", "writer-agent"))
        os.environ.pop(statecache.CACHE_DISABLED_ENV, None)
        try:
            first = writer.tool_pair_state()
            self.assertNotIn("unchanged", first)
            self.assertTrue(
                any("not delivered" in b for b in first["closing_blocked_by"]),
                f"pre-push the floor must block: {first['closing_blocked_by']}")
            git(work, "push", "-q", "origin", "master")
            second = writer.tool_pair_state()
            # Full answer required twice: the verdict's Mutation 1 (snapshot
            # gate back to verdicts-only) makes the second call answer the
            # stub, because nothing else in state moved across the push.
            self.assertNotIn(
                "unchanged", second,
                "post-push pair_state answered the stub over a moved floor: "
                "the fingerprint never saw the push (Trap 2)")
            self.assertFalse(
                any("not delivered" in b for b in second["closing_blocked_by"]),
                f"post-push the floor blocker must be gone: "
                f"{second['closing_blocked_by']}")
            # The two full answers must differ in the floor blocker itself:
            # guards against a future cache that returns full-but-stale.
            self.assertNotEqual(
                sorted(first["closing_blocked_by"]),
                sorted(second["closing_blocked_by"]))
        finally:
            os.environ[statecache.CACHE_DISABLED_ENV] = "1"

    def test_observe_floor_blocks(self) -> None:
        """Observe counts, not commands (PROTOCOL §10b): an undelivered HEAD
        blocks at observe with the same push message as A3.

        No `or True` — that literal made Mutation 2 pass green. Real repo
        with a bare local origin, one commit, no push: the floor answers
        undelivered and _done_blockers carries it at observe exactly as at
        auto. Fails if the floor is ever level-gated or the message rots.
        """
        import server as _server
        git(Path(self._tmp.name), "init", "-q", "--bare", "obs-origin.git")
        git(Path(self._tmp.name), "clone", "-q",
            str(Path(self._tmp.name) / "obs-origin.git"), "obs")
        work = Path(self._tmp.name) / "obs"
        git(work, "config", "user.email", "t@t.t")
        git(work, "config", "user.name", "t")
        (work / "f.txt").write_text("x\n", encoding="utf-8")
        git(work, "add", "f.txt")
        git(work, "commit", "-qm", "init")
        git(work, "push", "-q", "origin", "master")
        (work / "f.txt").write_text("y\n", encoding="utf-8")
        git(work, "add", "f.txt")
        git(work, "commit", "-qm", "advance")
        store = _server.Store(work / ".pair")
        writer = _server.PairSession("writer-agent", store)
        store.mutate(lambda s: (s.__setitem__("writer", "writer-agent"),
                                s.__setitem__("autonomy", "observe")))
        blockers = writer._done_blockers(store.read())
        hits = [b for b in blockers if "not delivered" in b]
        self.assertEqual(len(hits), 1,
                         f"observe must carry the undelivered blocker: {blockers}")
        self.assertIn("git push", hits[0])

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

    def test_explicit_head_pins_named_revision_not_live_tree(self) -> None:
        """Round 97: the re-pin after a branch rebuild must land on the named head."""
        import review
        self.writer.tool_pair_state()
        self.peer.tool_pair_state()
        with mock.patch.object(review, "resolve_head", return_value="b" * 40), \
                mock.patch.object(review, "head_files", return_value=["a.py"]), \
                mock.patch.object(review, "dirty_files", return_value=[]), \
                mock.patch.object(review, "dirty_hash", return_value="d123"):
            self.peer.tool_pair_note(kind="verdict", text="re-pinned",
                                     head=("b" * 40)[:12])
        verdicts = self.store.read().get("verdicts", [])
        self.assertTrue(verdicts, "the note must pin a verdict record")
        self.assertEqual("b" * 40, verdicts[-1]["rev"]["head"],
                         f"the record must anchor the named head: {verdicts[-1]['rev']}")

    def test_explicit_head_on_unreviewed_tree_stays_stale(self) -> None:
        """C1: the anchor is only an anchor — staleness still reads content."""
        import review
        self.writer.tool_pair_state()
        self.peer.tool_pair_state()
        with mock.patch.object(review, "resolve_head", return_value="b" * 40), \
                mock.patch.object(review, "head_files", return_value=["a.py"]), \
                mock.patch.object(review, "dirty_files", return_value=[]), \
                mock.patch.object(review, "dirty_hash", return_value="d123"):
            self.peer.tool_pair_note(kind="verdict", text="pinned elsewhere",
                                     head="b" * 40)
        with mock.patch.object(review, "git_head", return_value="c" * 40), \
                mock.patch.object(review, "changed_between", return_value=["a.py"]), \
                mock.patch.object(review, "dirty_files", return_value=[]):
            blockers = self.writer._done_blockers(self.store.read())
        self.assertTrue(any("stale" in b for b in blockers),
                        f"an unreviewed anchor must stay stale: {blockers}")

    def test_unresolvable_head_fails_loudly(self) -> None:
        """An explicit head git cannot resolve must refuse, never bind elsewhere."""
        self.writer.tool_pair_state()
        self.peer.tool_pair_state()
        with self.assertRaises(ValueError):
            self.peer.tool_pair_note(kind="verdict", text="bad anchor",
                                     head="no-such-rev-xyz")


if __name__ == "__main__":
    unittest.main(verbosity=2)
