"""Тесты для session_facts.py: детерминированный снимок дерева.

Один тест-файл на один исходник: `session_facts.py` -> `test_session_facts.py`
"""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import session_facts
from session_facts import FactsError


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=repo,
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


class TempRepo:
    """Пустой git-репозиторий во временном каталоге."""

    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        git(self.root, "init", "-q")
        git(self.root, "config", "user.email", "t@t")
        git(self.root, "config", "user.name", "t")
        (self.root / "base.txt").write_text("base\n", encoding="utf-8")
        git(self.root, "add", "base.txt")
        git(self.root, "commit", "-qm", "base")

    def cleanup(self) -> None:
        self.tmp.cleanup()


class TestParseStatus(unittest.TestCase):
    def test_modified_and_untracked_split(self) -> None:
        touched, untracked = session_facts.parse_status(
            " M src/a.py\nA  src/b.py\n?? new.txt\n")
        self.assertEqual(touched, ["src/a.py", "src/b.py"])
        self.assertEqual(untracked, ["new.txt"])

    def test_rename_takes_new_side(self) -> None:
        touched, _ = session_facts.parse_status("R  old.py -> new.py\n")
        self.assertEqual(touched, ["new.py"])


class TestCollectFacts(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = TempRepo()

    def tearDown(self) -> None:
        self.repo.cleanup()

    def test_clean_tree_empty(self) -> None:
        facts = session_facts.collect_facts(self.repo.root)
        self.assertEqual(facts["version"], session_facts.FACTS_VERSION)
        self.assertEqual(facts["touched"], [])
        self.assertEqual(facts["untracked"], [])
        self.assertEqual(facts["mock_flags"], [])
        self.assertEqual(len(facts["head"]), 40)

    def test_touched_matches_porcelain(self) -> None:
        (self.repo.root / "base.txt").write_text("base\nmore\n", encoding="utf-8")
        (self.repo.root / "fresh.txt").write_text("x\n", encoding="utf-8")
        facts = session_facts.collect_facts(self.repo.root)
        self.assertEqual(facts["touched"], ["base.txt"])
        self.assertEqual(facts["untracked"], ["fresh.txt"])
        self.assertIn("base.txt", facts["diff_stat"])

    def test_mock_patterns_flagged_with_coordinates(self) -> None:
        web = self.repo.root / "web" / "app.js"
        web.parent.mkdir()
        web.write_text("const CHAPTERS = [];\n", encoding="utf-8")
        git(self.repo.root, "add", "web/app.js")
        git(self.repo.root, "commit", "-qm", "web")
        web.write_text(
            "const CHAPTERS = INITIAL_CHAPTERS;\n"
            "const id = 'mock-abc123';\n"
            "if (name.includes('len(users))')) { xp++; }\n",
            encoding="utf-8")
        facts = session_facts.collect_facts(self.repo.root)
        kinds = {(f["pattern"], f["file"]) for f in facts["mock_flags"]}
        self.assertIn(("initial-static", "web/app.js"), kinds)
        self.assertIn(("mock-id", "web/app.js"), kinds)
        self.assertIn(("includes-len", "web/app.js"), kinds)
        for flag in facts["mock_flags"]:
            self.assertGreaterEqual(flag["line"], 1)
            self.assertFalse(flag["test_path"])

    def test_test_paths_marked_not_combat(self) -> None:
        path = self.repo.root / "tests" / "test_x.py"
        path.parent.mkdir()
        path.write_text("x = mock('thing')\n", encoding="utf-8")
        facts = session_facts.collect_facts(self.repo.root)
        self.assertEqual(len(facts["mock_flags"]), 1)
        self.assertTrue(facts["mock_flags"][0]["test_path"])

    def test_not_a_repo_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FactsError):
                session_facts.collect_facts(tmp)


class TestLoadPatterns(unittest.TestCase):
    def test_missing_config_falls_back_to_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(session_facts.load_patterns(tmp),
                             session_facts.MOCK_PATTERNS)

    def test_broken_config_falls_back_to_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "anti-mock.json").write_text("{broken",
                                                   encoding="utf-8")
            self.assertEqual(session_facts.load_patterns(tmp),
                             session_facts.MOCK_PATTERNS)

    def test_config_overrides_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "anti-mock.json").write_text(json.dumps({"patterns": [
                {"name": "x-stub", "regex": "TODO-STUB",
                 "reason": "custom"}]}), encoding="utf-8")
            loaded = session_facts.load_patterns(tmp)
            self.assertEqual([n for n, _, _ in loaded], ["x-stub"])
            flags = session_facts.scan_text("app/x.py", 1,
                                            ["x = TODO-STUB"], loaded)
            self.assertEqual(flags[0]["pattern"], "x-stub")

    def test_config_itself_is_gate_exempt(self) -> None:
        self.assertTrue(session_facts.is_gate_exempt("anti-mock.json"))
        self.assertEqual(session_facts.scan_text("anti-mock.json", 1,
                                                 ["x = 'mock-abc'"]), [])


class TestRoundTrip(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = TempRepo()
        self.tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.repo.cleanup()
        self.tmp.cleanup()

    def test_save_load_and_record_run(self) -> None:
        facts = session_facts.collect_facts(self.repo.root)
        target = session_facts.save_facts(self.state_dir, facts)
        loaded = session_facts.load_facts(target)
        self.assertEqual(loaded["head"], facts["head"])
        updated = session_facts.record_test_run(
            target, "python3 mcp/pair_mailbox/test_session_facts.py",
            0, "9 passed")
        self.assertEqual(len(updated["test_runs"]), 1)
        self.assertEqual(updated["test_runs"][0]["exit"], 0)

    def test_broken_facts_fails_closed(self) -> None:
        bad = self.state_dir / session_facts.FACTS_FILE
        bad.write_text("{nope", encoding="utf-8")
        with self.assertRaises(FactsError):
            session_facts.load_facts(bad)


if __name__ == "__main__":
    unittest.main()
