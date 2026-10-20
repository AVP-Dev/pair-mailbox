"""Which room is mine? One resolver, so every entry point answers alike.

Round 127. The rule "the project root owns the state, never the inherited
environment" was implemented in two places and contradicted in three, and the
cost was never the bugs — it was that the rule had no single place, so the
next entry point would get it wrong the same way.

Measured, from a project subdirectory:
- `bin/pair-duo` took a foreign `PAIR_STATE` on its face, wrote a WRITER into
  that room and launched the panes bound to it;
- `bin/pairpilot` created a second room under the subdirectory and watched
  THAT one — and the nearer `.pair` wins every later walk-up, so the second
  room hides the real one from the next tool that resolves by walking up.

These tests cover the resolver itself, so every entry point inherits the
contract rather than re-implementing it.

Run: python3 mcp/pair_mailbox/test_state_resolve.py
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))

from state_resolve import resolve_state_dir, walk_up_state  # noqa: E402


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=str(path), check=True, timeout=30)
    return path


class ResolveStateDirCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.project = _git_repo(base / "proj")
        self.state = self.project / ".pair"
        self.state.mkdir(parents=True)
        (self.state / "state.json").write_text(json.dumps(
            {"round": 3, "mode": "fix", "writer": "omp", "phase": "implement",
             "roles": {}, "tasks": [], "seq": 0}), encoding="utf-8")
        self.sub = self.project / "sub"
        self.sub.mkdir()
        self.foreign = base / "foreign" / ".pair"
        self.foreign.mkdir(parents=True)
        (self.foreign / "state.json").write_text("{}", encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_the_room_underfoot_is_the_answer(self) -> None:
        room, reason = resolve_state_dir(env={}, cwd=self.sub)
        self.assertEqual(self.state.resolve(), room.resolve(),
                         f"the room underfoot is the room, got {room} ({reason})")

    def test_a_subdirectory_does_not_create_a_second_room(self) -> None:
        """The litter: `allow_create=True` turned a relative default into a
        second room in the subdirectory."""
        self.assertFalse((self.sub / ".pair").exists())
        resolve_state_dir(env={}, cwd=self.sub)
        self.assertFalse((self.sub / ".pair").exists(),
                         "resolving must not create a room underfoot")

    def test_a_foreign_inherited_room_is_refused(self) -> None:
        env = {"PAIR_STATE": str(self.foreign)}
        room, reason = resolve_state_dir(env=env, cwd=self.sub)
        self.assertEqual(self.state.resolve(), room.resolve(),
                         f"the foreign room must not be adopted ({reason})")
        self.assertIn("refused", reason)
        self.assertIn(str(self.foreign), reason,
                      "the refusal must name the room it refused")
        self.assertIn(str(self.state), reason,
                      "the refusal must name the room it used instead")

    def test_trust_env_keeps_the_foreign_room_on_purpose(self) -> None:
        """The deliberate escape stays available."""
        env = {"PAIR_STATE": str(self.foreign), "PAIR_TRUST_ENV": "1"}
        room, reason = resolve_state_dir(env=env, cwd=self.sub)
        self.assertEqual(self.foreign.resolve(), room.resolve(),
                         f"PAIR_TRUST_ENV=1 must keep the room the operator named ({reason})")

    def test_an_explicit_state_beats_the_environment(self) -> None:
        """The flag is the operator speaking; the environment is a leftover."""
        env = {"PAIR_STATE": str(self.foreign)}
        room, _ = resolve_state_dir(explicit=str(self.state), env=env, cwd=self.sub)
        self.assertEqual(self.state.resolve(), room.resolve(),
                         "an explicit --state must win")

    def test_the_walk_up_is_bounded_by_the_git_root(self) -> None:
        """Without the bound, someone else's ~/work/.pair latches on from a
        subdirectory."""
        outer = self.project.parent
        outside = outer / "outside" / ".pair"
        outside.mkdir(parents=True)
        (outside / "state.json").write_text("{}", encoding="utf-8")
        deep = self.sub / "deep"
        deep.mkdir()
        found = walk_up_state(deep)
        self.assertIsNotNone(found)
        self.assertNotEqual(outside.resolve(), found.resolve(),
                            "the walk-up must stop at the git root")

    def test_a_trusted_inherited_room_counts_even_with_no_room_underfoot(self) -> None:
        """The escape hatch must work where there is no room to fall back on.

        Found by CI (all five Python versions), not by the local run: the local
        tree had a `.pair` above the temp dir, so the walk-up always found
        something and the early return never fired. On a clean machine there is
        no room underfoot, and the resolver returned the fallback WITHOUT
        consulting the inherited room — so PAIR_TRUST_ENV=1 was ignored exactly
        where it was the only thing that could answer.
        """
        with tempfile.TemporaryDirectory() as tmp:
            bare = Path(tmp) / "bare"
            bare.mkdir()
            chosen = Path(tmp) / "chosen"
            env = {"PAIR_STATE": str(chosen), "PAIR_TRUST_ENV": "1"}
            room, reason = resolve_state_dir(env=env, cwd=bare,
                                            no_walk_up_above=Path(tmp))
            self.assertEqual(chosen.resolve(), room.resolve(),
                             f"a trusted inherited room must be honoured ({reason})")

    def test_an_untrusted_inherited_room_is_refused_with_no_room_underfoot(self) -> None:
        """The refusal holds where there is no room underfoot either: the
        fallback is the caller's own project, never a foreign room."""
        with tempfile.TemporaryDirectory() as tmp:
            bare = Path(tmp) / "bare"
            bare.mkdir()
            foreign = Path(tmp) / "foreign"
            env = {"PAIR_STATE": str(foreign)}
            room, reason = resolve_state_dir(env=env, cwd=bare,
                                            no_walk_up_above=Path(tmp))
            self.assertNotEqual(foreign.resolve(), room.resolve(),
                                f"a foreign room must not be adopted ({reason})")
            self.assertEqual((bare / ".pair").resolve(), room.resolve(),
                             "the fallback is the caller's own project")


if __name__ == "__main__":
    unittest.main(verbosity=2)
