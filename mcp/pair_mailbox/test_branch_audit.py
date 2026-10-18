"""Tests for branch_audit.py — the §20 janitor (PROTOCOL §20, janitor contract).

`gh` is mocked because the contract is *what the janitor does with the answers*,
and the answers come from GitHub. The refused directions are the product here:
each one is a case a sweep would get wrong by being useful.

The branch list is NOT mocked: it comes from real `git for-each-ref` in a temp
repo, because a janitor that mis-reads git is a janitor that deletes the wrong
thing, and that failure mode is invisible to a mocked ref list.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import importlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import branch_audit  # noqa: E402
import branch_classes  # noqa: E402

# The unpatched `subprocess.run`, captured before any mock replaces it: the
# janitor's git calls must execute for real, because the branch list it audits
# comes from git, not from a stub. Patching the module object cannot help —
# `importlib` hands back the same module, patched or not.
_REAL_RUN = subprocess.run


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return _REAL_RUN(["git", *args], cwd=str(cwd), capture_output=True,
                     text=True)


class _FakeGH:
    """The gh the janitor would have called, as a table.

    `heads` maps branch -> the head SHA GitHub would report. It must be the
    branch's real SHA: the janitor compares the remote PR head against the local
    ref, and a fake that always answers the same SHA makes every branch look
    push-past-its-merge. The `repo` argument lets it read git for the real SHA.

    `raise_on` makes one branch unanswerable: the janitor must refuse it, not
    skip it silently.
    """

    sha_of = staticmethod(lambda repo_dir, branch: _git_rev(repo_dir, branch))

    def __init__(self, states: dict[str, tuple[str, str]], raise_on=None,
                 repo: Path | None = None):
        self.states = states
        self.raise_on = raise_on
        self.repo = repo

    def __call__(self, cmd, *args, **kwargs):
        """`subprocess.run(cmd, ...)`: the command is the first positional, a
        list — not the argv flat. Comparing against `["gh"]` instead of `"gh"`
        silently passes every call through to the real binary, which is how a
        test suite starts shelling out to `gh` in CI."""
        cmd = list(cmd)
        if cmd[:1] != ["gh"]:
            return _REAL_RUN([*cmd, *args], **kwargs)
        head = cmd[cmd.index("--head") + 1] if "--head" in cmd else None
        if head == self.raise_on and cmd[:3] == ["gh", "pr", "list"]:
            return subprocess.CompletedProcess(cmd, 1, "",
                                               "gh: connection refused")
        real_head = ""
        if head and self.repo:
            proc = _REAL_RUN(
                ["git", "rev-parse", f"refs/remotes/origin/{head}"],
                cwd=str(self.repo), capture_output=True, text=True)
            real_head = proc.stdout.strip() if proc.returncode == 0 else ""
        state, pr = self.states.get(head or "", ("NO_PR", ""))
        if pr or state != "NO_PR":
            row = {"number": pr, "state": state,
                   "mergeCommit": {"oid": "f" * 40},
                   "headRefOid": real_head or "f" * 40}
            return subprocess.CompletedProcess(cmd, 0, json.dumps([row]), "")
        if cmd[:3] == ["gh", "pr", "view"]:
            # the janitor asks by PR number, and `--head` is absent: resolve the
            # number back to the branch this fake knows, or the head cannot be
            # matched and every merged case reads as pushed-past-its-merge.
            pr_no = cmd[3] if len(cmd) > 3 else ""
            for branch, (_state, branch_pr) in self.states.items():
                if branch_pr == pr_no:
                    head = branch
                    break
            else:
                head = None
            resolved = ""
            if head and self.repo:
                proc = _REAL_RUN(
                    ["git", "rev-parse", f"refs/remotes/origin/{head}"],
                    cwd=str(self.repo), capture_output=True, text=True)
                resolved = proc.stdout.strip() if proc.returncode == 0 else ""
            return subprocess.CompletedProcess(cmd, 0, resolved or "f" * 40, "")
        return subprocess.CompletedProcess(cmd, 0, "[]", "")


class TestBranchAudit(unittest.TestCase):
    def _repo(self, tmp: str, branches: list[str]) -> Path:
        origin = Path(tmp) / "origin.git"
        _git(Path(tmp), "init", "-q", "--bare", str(origin))
        work = Path(tmp) / "work"
        work.mkdir()
        _git(work, "init", "-q", "-b", "master")
        _git(work, "config", "user.email", "t@t")
        _git(work, "config", "user.name", "t")
        (work / "f.txt").write_text("1", encoding="utf-8")
        _git(work, "add", "-A")
        _git(work, "commit", "-q", "-m", "base")
        _git(work, "remote", "add", "origin", str(origin))
        for branch in branches:
            _git(work, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")
        _git(work, "fetch", "-q", "origin")
        _git(work, "remote", "set-head", "origin", "master")
        return work

    def _audit(self, repo: Path, gh):
        with mock.patch.object(branch_audit.subprocess, "run", gh):
            return branch_audit.audit(repo)

    def _verdict(self, rows: list[dict], branch: str) -> str:
        for row in rows:
            if row["branch"] == branch:
                return row["verdict"]
        raise AssertionError(f"{branch} not audited")

    def test_trunk_is_never_a_delete_candidate_even_when_merged(self) -> None:
        """A merged `master` is not a merged branch; the class owns the verdict."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp, ["master"])
            gh = _FakeGH({"master": ("MERGED", "7")}, repo=repo)
            rows, refusals = self._audit(repo, gh)
            self.assertEqual(self._verdict(rows, "master"), "KEEP")
            self.assertEqual(refusals, [])

    def test_strategic_is_never_deleted_by_a_robot(self) -> None:
        """§20: release and support lines outlive their PRs on purpose."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp, ["master", "release/1.2"])
            gh = _FakeGH({"release/1.2": ("MERGED", "8")}, repo=repo)
            rows, _ = self._audit(repo, gh)
            self.assertEqual(self._verdict(rows, "release/1.2"), "KEEP")

    def test_personal_branches_are_swept_never_deleted(self) -> None:
        """`stale` is a claim about someone's unfinished thought."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp, ["master", "users/andy/wip"])
            gh = _FakeGH({"users/andy/wip": ("NO_PR", "")}, repo=repo)
            rows, _ = self._audit(repo, gh)
            self.assertEqual(self._verdict(rows, "users/andy/wip"), "KEEP")

    def test_merged_ephemeral_is_the_only_merge_row(self) -> None:
        """The one case that may end in a delete, and it names its PR."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp, ["master", "feature/login"])
            gh = _FakeGH({"feature/login": ("MERGED", "9")}, repo=repo)
            rows, refusals = self._audit(repo, gh)
            self.assertEqual(self._verdict(rows, "feature/login"), "MERGE")
            self.assertEqual(refusals, [])
            row = [r for r in rows if r["branch"] == "feature/login"][0]
            self.assertEqual(row["pr"], "9")

    def test_open_ephemeral_is_stale_not_deleted(self) -> None:
        """Someone's work in progress: deleting it destroys the only copy."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp, ["master", "feature/wip"])
            gh = _FakeGH({"feature/wip": ("OPEN", "10")}, repo=repo)
            rows, _ = self._audit(repo, gh)
            self.assertEqual(self._verdict(rows, "feature/wip"), "STALE")

    def test_closed_unmerged_ephemeral_is_stale_not_deleted(self) -> None:
        """Round-91 shape: the PR was closed without merging, and the branch is
        the only surviving copy. A closed PR is not a merged one."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp, ["master", "fix/race"])
            gh = _FakeGH({"fix/race": ("CLOSED", "11")}, repo=repo)
            rows, _ = self._audit(repo, gh)
            self.assertEqual(self._verdict(rows, "fix/race"), "STALE")

    def test_branch_with_no_pr_is_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp, ["master", "chore/deps"])
            gh = _FakeGH({}, repo=repo)
            rows, _ = self._audit(repo, gh)
            self.assertEqual(self._verdict(rows, "chore/deps"), "STALE")

    def test_accumulator_is_an_anomaly_and_never_deleted(self) -> None:
        """§20 rule 4 names accumulators an antipattern — reported as ANOMALY, so
        a human removes it with a reason; tidying the anomaly is how a bad name
        becomes invisible."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp, ["master", "dev"])
            gh = _FakeGH({}, repo=repo)
            rows, _ = self._audit(repo, gh)
            self.assertEqual(self._verdict(rows, "dev"), "ANOMALY")
            row = [r for r in rows if r["branch"] == "dev"][0]
            self.assertIn("antipattern", row["reason"])

    def test_unknown_name_is_an_anomaly_and_kept(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp, ["master", "qwerty"])
            gh = _FakeGH({}, repo=repo)
            rows, _ = self._audit(repo, gh)
            self.assertEqual(self._verdict(rows, "qwerty"), "ANOMALY")

    def test_gh_refusal_is_reported_and_trusts_nothing(self) -> None:
        """The one direction a sweep must not pass: gh cannot answer, so the
        branch's state is unknown, and unknown is never a delete candidate."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp, ["master", "feature/login"])
            gh = _FakeGH({"feature/login": ("MERGED", "9")},
                         raise_on="feature/login", repo=repo)
            rows, refusals = self._audit(repo, gh)
            self.assertEqual(self._verdict(rows, "feature/login"), "UNKNOWN")
            self.assertEqual(len(refusals), 1)
            self.assertIn("feature/login", refusals[0])

    def test_head_mismatch_is_refused_not_resolved(self) -> None:
        """The remote's head differs from what the local ref recorded: that is a
        branch pushed past its merge, and the janitor has no opinion to offer."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp, ["master", "feature/login"])
            gh = _FakeGH({"feature/login": ("MERGED", "9")}, repo=repo)
            with mock.patch.object(branch_audit.subprocess, "run", gh), \
                 mock.patch.object(branch_audit, "gh_pr_head",
                                   lambda repo_dir, pr: "a" * 40):
                rows, _ = branch_audit.audit(repo)
            self.assertEqual(self._verdict(rows, "feature/login"), "ANOMALY")
            row = [r for r in rows if r["branch"] == "feature/login"][0]
            self.assertIn("refusing", row["reason"])

    def test_the_table_is_shared_not_copied(self) -> None:
        """The janitor and the checker must agree on what a name means, or the
        janitor deletes what CI just called legal."""
        self.assertIs(branch_audit.classify, branch_classes.classify)

    def test_delete_writes_transcript_and_removes_only_that_branch(self) -> None:
        """The one destructive direction, proven: the branch leaves origin, the
        transcript names what was known (head + PR), and no other branch is
        touched."""
        seen: list[tuple[str, dict]] = []

        class _Store:
            def append_transcript(self, kind: str, role: str, **fields) -> None:
                seen.append((kind, fields))

        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp, ["master", "feature/login"])
            sha = _git(repo, "rev-parse", "refs/remotes/origin/feature/login")
            row = {"branch": "feature/login", "class": "ephemeral",
                   "sha": sha.stdout.strip(), "verdict": "MERGE", "pr": "9",
                   "reason": ""}
            rc = branch_audit.delete_merged(repo, row, _Store(), "writer")
            self.assertEqual(rc, 0)
            gone = _git(repo, "ls-remote", "--heads", "origin",
                        "refs/heads/feature/login")
            self.assertEqual(gone.stdout.strip(), "", "branch must leave origin")
            kept = _git(repo, "ls-remote", "--heads", "origin", "refs/heads/master")
            self.assertIn("refs/heads/master", kept.stdout, "master must stay")
            self.assertEqual(len(seen), 1)
            kind, fields = seen[0]
            self.assertEqual(kind, "branch_deleted")
            self.assertEqual(fields["branch"], "feature/login")
            self.assertEqual(fields["pr"], "9")
            self.assertEqual(fields["head_sha"], row["sha"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
