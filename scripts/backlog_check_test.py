"""Tests for scripts/backlog-check.py (PROTOCOL §9 backlog lifecycle).

Behavioural: real card files in a temporary tree, the real script run over
them. A parser mocked into agreement with itself would prove that the parser
agrees with the parser, and the rule this gate exists for is exactly the one
a mock cannot see — a card in the repository that says `status: fixed` while
the directory claims to hold open debt only.

Same shape as scripts/branch_class_check_test.py: no network, local files.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "backlog-check.py"

CARD = """---
title: A real defect
found: 2026-10-20
severity: medium
round: 130
status: open
---

The body of the card.
"""


def run(root: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root), *extra],
        capture_output=True, text=True, timeout=60,
    )


class BacklogCheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.directory = self.root / "docs" / "backlog"
        self.directory.mkdir(parents=True)
        self.addCleanup(self._tmp.cleanup)

    def card(self, name: str, **fields: str) -> Path:
        """Write a card, overriding any frontmatter field."""
        body = CARD
        for key, value in fields.items():
            lines = body.splitlines()
            for index, line in enumerate(lines):
                if line.startswith(f"{key}:"):
                    lines[index] = f"{key}: {value}"
                    break
            else:
                raise AssertionError(f"the fixture has no {key} field")
            body = "\n".join(lines) + "\n"
        path = self.directory / name
        path.write_text(body, encoding="utf-8")
        return path

    def test_a_well_formed_card_passes(self) -> None:
        self.card("good.md")
        proc = run(self.root)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("1 card(s)", proc.stdout)

    def test_a_closed_card_is_refused_and_named(self) -> None:
        self.card("closed.md", status="fixed")
        proc = run(self.root)
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("closed.md", proc.stdout)
        self.assertIn("status", proc.stdout)
        self.assertIn("not open", proc.stdout)

    def test_a_missing_field_is_refused_and_named(self) -> None:
        path = self.card("nofield.md")
        text = path.read_text(encoding="utf-8").replace("severity: medium\n", "")
        path.write_text(text, encoding="utf-8")
        proc = run(self.root)
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("severity", proc.stdout)
        self.assertIn("missing", proc.stdout)

    def test_an_empty_field_is_refused(self) -> None:
        self.card("empty.md", title="")
        proc = run(self.root)
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("title", proc.stdout)
        self.assertIn("empty", proc.stdout)

    def test_an_extra_field_is_refused(self) -> None:
        path = self.card("extra.md")
        text = path.read_text(encoding="utf-8").replace(
            "status: open", "status: open\nfixed_by: round 129")
        path.write_text(text, encoding="utf-8")
        proc = run(self.root)
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("fixed_by", proc.stdout)

    def test_a_file_without_frontmatter_is_refused(self) -> None:
        (self.directory / "prose.md").write_text("# just prose\n", encoding="utf-8")
        proc = run(self.root)
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("prose.md", proc.stdout)
        self.assertIn("frontmatter", proc.stdout)

    def test_an_unknown_severity_is_refused(self) -> None:
        self.card("sev.md", severity="urgent")
        proc = run(self.root)
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("severity", proc.stdout)

    def test_a_round_that_is_not_a_number_is_refused(self) -> None:
        self.card("round.md", round="one-thirty")
        proc = run(self.root)
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("round", proc.stdout)

    def test_a_round_of_zero_is_refused(self) -> None:
        self.card("zero.md", round="0")
        proc = run(self.root)
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("round", proc.stdout)

    def test_a_found_that_is_not_a_date_is_refused(self) -> None:
        self.card("found.md", found="last tuesday")
        proc = run(self.root)
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("found", proc.stdout)

    def test_both_date_shapes_are_accepted(self) -> None:
        self.card("day.md", found="2026-10-20")
        self.card("instant.md", found="2026-10-20T07:50Z")
        proc = run(self.root)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_the_guide_is_not_a_card(self) -> None:
        (self.directory / "README.md").write_text(
            "Here only open debt lives.\n", encoding="utf-8")
        proc = run(self.root)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_a_file_that_is_not_markdown_is_refused(self) -> None:
        (self.directory / "notes.txt").write_text("stray\n", encoding="utf-8")
        proc = run(self.root)
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("notes.txt", proc.stdout)

    def test_a_missing_directory_is_not_a_pass(self) -> None:
        empty = self.root / "elsewhere"
        empty.mkdir()
        proc = run(empty)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("refusing", proc.stderr)

    def test_an_empty_directory_is_a_pass(self) -> None:
        proc = run(self.root)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("0 card(s)", proc.stdout)

    def test_the_index_counts_the_cards_on_disk(self) -> None:
        self.card("one.md")
        self.card("two.md", severity="high")
        proc = run(self.root, "--index")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("2 open card(s)", proc.stdout)
        self.assertIn("one.md", proc.stdout)
        self.assertIn("two.md", proc.stdout)

    def test_the_index_names_the_rounds_without_a_round_card(self) -> None:
        self.card("one.md")
        proc = run(self.root, "--index")
        self.assertIn("130", proc.stdout)
        self.assertIn("no round card", proc.stdout)

    def test_the_archive_comes_from_git(self) -> None:
        self._git("init", "-q")
        self.card("gone.md")
        self._git("add", "-A")
        self._git("commit", "-qm", "card")
        self._git("rm", "-q", "docs/backlog/gone.md")
        self._git("commit", "-qm", "close it")
        proc = run(self.root, "--closed")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("gone.md", proc.stdout)

    def _git(self, *args: str) -> None:
        proc = subprocess.run(["git", *args], cwd=str(self.root),
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
