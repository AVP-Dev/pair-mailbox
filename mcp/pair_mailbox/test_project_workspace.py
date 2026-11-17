from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from project_workspace import launch_state
from store_core import Store


class ProjectWorkspace(unittest.TestCase):
    def test_role_workspace_wins_over_a_stale_daemon_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            (repo / ".git").mkdir()
            worktree = Path(tmp) / "linked-worktree"
            worktree.mkdir()
            git_dir = repo / ".git" / "worktrees" / "linked-worktree"
            git_dir.mkdir(parents=True)
            (git_dir / "commondir").write_text("../..\n", encoding="utf-8")
            (worktree / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")
            store = Store(repo / ".pair", allow_create=True)
            from server import PairSession, ProtocolError
            session = PairSession("codex", store)

            session.tool_pair_state(workspace=str(worktree))
            self.assertEqual(str(worktree.resolve()), store.read()["roles"]["codex"]["workspace"])
            self.assertEqual(worktree.resolve(), store.record_role_workspace("codex", worktree))
            self.assertEqual(worktree.resolve(), store.project_root_for("codex"))
            with self.assertRaises(ProtocolError):
                session.tool_pair_state(workspace=str(Path(tmp) / "foreign"))

    def test_foreign_workspace_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            (repo / ".git").mkdir()
            foreign = Path(tmp) / "foreign"
            foreign.mkdir()
            (foreign / ".git").mkdir()
            store = Store(repo / ".pair", allow_create=True)

            self.assertIsNone(store.record_role_workspace("codex", foreign))
            self.assertEqual(repo.resolve(), store.project_root_for("codex"))

    def test_launch_state_records_workspace_for_an_existing_role(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            (project / ".pair").mkdir()
            store = Store(project / ".pair", allow_create=True)
            store._save(store.default_state())
            state = launch_state(project / ".pair", "codex", project)
            self.assertEqual(str(project), state["roles"]["codex"]["workspace"])

    def test_evidence_root_tracks_distinct_worktree_head(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            for key, value in (("user.email", "pair@test.local"), ("user.name", "Pair Test")):
                subprocess.run(["git", "config", key, value], cwd=repo, check=True)
            (repo / "file.txt").write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "add", "file.txt"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
            worktree = Path(tmp) / "worktree"
            subprocess.run(["git", "worktree", "add", "-qb", "work", str(worktree)],
                           cwd=repo, check=True)
            (worktree / "file.txt").write_text("worktree\n", encoding="utf-8")
            subprocess.run(["git", "commit", "-qam", "worktree"], cwd=worktree, check=True)
            head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=worktree,
                                  check=True, capture_output=True, text=True).stdout.strip()
            (worktree / "file.txt").write_text("dirty Work3 change\n", encoding="utf-8")

            store = Store(repo / ".pair", allow_create=True)
            from server import PairSession
            session = PairSession("writer-agent", store)
            session.tool_pair_state(workspace=str(worktree))
            session.tool_pair_evidence(
                kind="test", value="12 passed, exit 0")
            self.assertEqual(head, store.read()["evidence"][-1]["head"])
            fingerprint = session._git_fingerprint()
            self.assertEqual(head, fingerprint["head"])
            self.assertNotEqual("e3b0c44298fc1c14", fingerprint["dirty"])
            self.assertEqual(["file.txt"], fingerprint["files"])


if __name__ == "__main__":
    unittest.main()
