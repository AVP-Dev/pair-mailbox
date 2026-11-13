#!/usr/bin/env python3
"""Tests for scripts/protocol_parity_check.py.

Behavioural: the real script run over real files, plus temporary ones that
break the mirror in the three ways the mirror actually broke — a missing
section, a missing subsection, and a reordered section.

The live check matters most: it is the property the check exists for, measured
on the repository's own two files. A test with fixtures alone would pass on a
mirror that has drifted again.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "protocol_parity_check.py"
ORIGINAL = ROOT / "docs" / "PROTOCOL.md"
MIRROR = ROOT / "docs" / "PROTOCOL.ru.md"


class ProtocolParityCheckTest(unittest.TestCase):
    def run_check(self, original: Path, mirror: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--original", str(original),
             "--mirror", str(mirror)],
            capture_output=True, text=True, timeout=60)

    def test_the_live_mirror_matches_the_live_original(self) -> None:
        """The property the check exists for, on the repository's own files."""
        proc = self.run_check(ORIGINAL, MIRROR)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("PASS", proc.stdout)

    def test_a_missing_section_is_named(self) -> None:
        """The mirror lost §17 for years and nobody learned of it."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = root / "original.md"
            mirror = root / "mirror.md"
            original.write_text("## 1. One\n\n## 2. Two\n", encoding="utf-8")
            mirror.write_text("## 1. One\n", encoding="utf-8")
            proc = self.run_check(original, mirror)
            self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
            self.assertIn("2", proc.stdout)
            self.assertIn("missing", proc.stdout)

    def test_a_missing_subsection_is_named(self) -> None:
        """The §6 plan-gate subsection was the mirror's third real gap."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = root / "original.md"
            mirror = root / "mirror.md"
            original.write_text("## 1. One\n\n### 1a. Sub\n", encoding="utf-8")
            mirror.write_text("## 1. One\n", encoding="utf-8")
            proc = self.run_check(original, mirror)
            self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
            self.assertIn("subsection", proc.stdout)
            self.assertIn("1a. Sub", proc.stdout)

    def test_a_reordered_section_is_named(self) -> None:
        """Same sections, different order: the reader's map is wrong."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = root / "original.md"
            mirror = root / "mirror.md"
            original.write_text("## 1. One\n\n## 2. Two\n", encoding="utf-8")
            mirror.write_text("## 2. Two\n\n## 1. One\n", encoding="utf-8")
            proc = self.run_check(original, mirror)
            self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
            self.assertIn("order", proc.stdout)

    def test_an_unreadable_file_fails_closed(self) -> None:
        """A check that cannot look proves nothing: that is a failure."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = root / "original.md"
            original.write_text("## 1. One\n", encoding="utf-8")
            proc = self.run_check(original, root / "absent.md")
            self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
            self.assertIn("FAILED", proc.stdout + proc.stderr)

    def test_a_numbering_gap_in_both_files_is_not_a_failure(self) -> None:
        """§14-16 exist in neither file: the mirror must not invent them.

        The mirror mirrors the original. If the original skips a number, the
        mirror skips it too, and the check says nothing — otherwise the check
        would push for a section that the English original does not have.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = root / "original.md"
            mirror = root / "mirror.md"
            original.write_text("## 1. One\n\n## 17. Seventeen\n", encoding="utf-8")
            mirror.write_text("## 1. Один\n\n## 17. Семнадцать\n", encoding="utf-8")
            proc = self.run_check(original, mirror)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("PASS", proc.stdout)


if __name__ == "__main__":
    unittest.main()
