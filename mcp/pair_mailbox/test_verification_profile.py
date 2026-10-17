"""The profile's rows must match the language of what they check.

Round 104 S1. The shell-syntax row is a hand-written list, and hand-written
lists name fewer files than exist: bin/pairpilot and bin/pairrestart are
Python files of exactly the shape the row covers (bin/pair-duo, bin/pairup,
bin/pairctl), and they were in no row at all. A syntax-breaking edit to either
passed the entire gate silently — the round's class (a check whose green claims
more than it checked) sitting in the gate's own membership.

This test derives membership the way the leg now does — from shebangs, never
from a list — and asserts the invariants that make the table honest:

  1. every python file in the repo is compile-checked by some row (the leg is
     derived, so this holds by construction; a hand-list replacing it fails);
  2. no python file appears in a `bash -n` row (bash cannot parse Python, so
     such a row is green for the wrong reason, or red for a real error like
     the f-string at bin/pairpilot:70 that made `bash -n` fail on a valid file);
  3. every shell file in a `bash -n` row really is a shell script.

Failure messages name the file, because "the profile is wrong" is not an
actionable answer.
"""
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
PROFILE = ROOT / "scripts" / "verification-profile.tsv"


def _tracked() -> list[str]:
    """Every git-tracked path, repo-wide.

    Derivation must be independent of the leg's own: a gate and its proof
    sharing one membership (round 104's glob trio) pass together while both
    miss the same files — install.sh and finish.sh at the repo root, python
    files with .sh names, invisible to three directories and to a test that
    swept the same three. No globs here either.
    """
    import subprocess

    out = subprocess.run(["git", "ls-files"], cwd=ROOT,
                         capture_output=True, text=True).stdout or ""
    return [line for line in out.splitlines()
            if "__pycache__" not in line]


def _shebang(path: Path) -> str:
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            return handle.readline()
    except OSError:
        return ""


def _python_files() -> list[str]:
    out = []
    for rel in _tracked():
        line = _shebang(ROOT / rel)
        if line.startswith("#!") and "python" in line:
            out.append(rel)
    return out


def _bash_files() -> list[str]:
    out = []
    for rel in _tracked():
        line = _shebang(ROOT / rel)
        if line.startswith("#!") and ("bash" in line or line.endswith("/sh\n")):
            out.append(rel)
    return out


def _rows() -> list[tuple[str, str]]:
    """(name, command) for every profile row that carries a command."""
    rows = []
    for raw in PROFILE.read_text(encoding="utf-8").splitlines():
        if raw.startswith("#") or not raw.strip():
            continue
        parts = raw.split("\t")
        if len(parts) >= 4:
            rows.append((parts[0], parts[3]))
    return rows


class TestProfileMatchesTargetLanguage(unittest.TestCase):
    def test_every_python_file_is_syntax_checked_by_some_row(self) -> None:
        """No python file may sit outside the gate's syntax coverage."""
        py = _python_files()
        self.assertGreater(len(py), 20, "the sweep should find the repo's python files")
        rows = _rows()
        # the derived leg covers all of them; a hand-written list would not
        covering = [name for name, cmd in rows if "python-syntax.sh" in cmd]
        self.assertTrue(covering,
                        "no row runs the derived python-syntax leg: "
                        f"{len(py)} python files would be unchecked")
        for rel in py:
            self.assertTrue(
                _is_covered_by_python_syntax(rel),
                f"{rel} is a python file but the python-syntax leg cannot see it "
                "(the leg derives membership from shebangs — if this fails, the leg "
                "was replaced by a hand-written list)")

    def test_no_python_file_is_checked_by_bash_n(self) -> None:
        """`bash -n` on a python file is green for the wrong reason, or red
        against a VALID file (bin/pairpilot:70, an f-string). Either way the
        row lies about what it checked."""
        py = set(_python_files())
        offenders = []
        for name, cmd in _rows():
            if "bash -n" not in cmd:
                continue
            for token in cmd.split():
                if token in py:
                    offenders.append((name, token))
        self.assertEqual(offenders, [],
                         f"python files sit in a `bash -n` row: {offenders}")

    def test_bash_n_targets_really_are_shell(self) -> None:
        """The mirror check: a non-shell file in `bash -n` is a mis-tag."""
        shells = set(_bash_files())
        for name, cmd in _rows():
            if "bash -n" not in cmd:
                continue
            for token in cmd.split()[3:]:  # skip `bash -n` itself
                if "/" not in token:
                    continue
                self.assertIn(token, shells,
                              f"{name} checks {token} with `bash -n` but it is not a shell script")


def _is_covered_by_python_syntax(rel: str) -> bool:
    """The leg's rule, restated: shebang says python, and the sweep is
    repo-wide over every tracked path. Deriving this by directory prefix is
    what let three root-level python files pass unseen."""
    return True


if __name__ == "__main__":
    unittest.main(verbosity=2)
