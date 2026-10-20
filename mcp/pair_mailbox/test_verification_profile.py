"""The profile's rows must match the language of what they check.

Round 104 S1. The shell-syntax row was a hand-written list, and hand-written
lists name fewer files than exist: bin/pairpilot and bin/pairrestart are
Python files of exactly the shape the row covers (bin/pair-duo, bin/pairup,
bin/pairctl), and they were in no row at all. A syntax-breaking edit to either
passed the entire gate silently — the round's class (a check whose green claims
more than it checked) sitting in the gate's own membership.

Round 127. The same defect survived the rewrite, one level down: the
shell-syntax row still named 11 files while the repo tracked 32 shell
scripts, and the python leg derived its membership from shebangs alone, so 76
of the repo's 141 python files — every library module — were never compiled.
The guard test had the same blind spot as the gate it guarded: it derived
membership with the very filter under question, so it could not see the gap.

So the test now mirrors the CLAIM, not the filter:

  1. every tracked python file is compile-checked by the python leg — the
     claim is "every python file in the repo", and the test derives that set
     from the FILE TYPE (`.py` or a python shebang), with no filter the leg
     could inherit;
  2. every tracked shell script is syntax-checked by the shell leg — the
     claim is "every shell script in the repo", and the test derives that set
     from the SHEBANG INTERPRETER, again independently of the leg;
  3. no python file appears in a `bash -n` row (bash cannot parse Python, so
     such a row is green for the wrong reason, or red for a real error like
     the f-string at bin/pairpilot:70 that made `bash -n` fail on a valid file);
  4. the two legs are DERIVED, not listed — a leg that named its files by hand
     would pass 1 and 2 while covering less, so the test pins the legs' own
     membership to the tree rather than to a list in the profile.

Failure messages name the file, because "the profile is wrong" is not an
actionable answer.
"""
from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
PROFILE = ROOT / "scripts" / "verification-profile.tsv"
PYTHON_LEG = ROOT / "scripts" / "python-syntax.sh"
SHELL_LEG = ROOT / "scripts" / "shell-syntax.sh"


def _tracked() -> list[str]:
    """Every git-tracked path, repo-wide.

    Derivation must be independent of the leg's own: a gate and its proof
    sharing one membership (round 104's glob trio) pass together while both
    miss the same files — install.sh and finish.sh at the repo root, python
    files with .sh names, invisible to three directories and to a test that
    swept the same three. No globs here either.
    """
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
    """Every tracked python file: a `.py` module, or a script with a python
    shebang.

    The file type decides, not the shebang: 76 of the repo's 141 python files
    are library modules with no shebang at all, and a membership rule that
    looked only at the shebang left every one of them unchecked (round 127).
    The two marks are both real python-ness, and both are covered.
    """
    out = []
    for rel in _tracked():
        line = _shebang(ROOT / rel)
        if rel.endswith(".py") or (line.startswith("#!") and "python" in line):
            out.append(rel)
    return out


def _shell_files() -> list[str]:
    """Every tracked shell script: a shebang that names a shell.

    The extension is not the mark — install.sh and finish.sh are python files
    carrying .sh names, and bin/pair-duo, bin/pairctl and bin/pairup are shell
    files with no extension at all. The shebang interpreter is the one mark
    that holds for both directions.
    """
    out = []
    for rel in _tracked():
        line = _shebang(ROOT / rel).strip()
        if not line.startswith("#!"):
            continue
        if "bash" in line or line.endswith("/sh") or line.endswith(" sh"):
            out.append(rel)
    return out


def _leg_members(leg: Path) -> set[str]:
    """The files a leg actually checks, read out of the leg itself.

    The leg prints the count it checked; this asks it WHICH ones by running it
    with a marker that makes it list them. A leg that grew a hand-written list
    would then be visible here — the point of the check.
    """
    out = subprocess.run([str(leg), "--list"], cwd=ROOT,
                         capture_output=True, text=True)
    if out.returncode != 0:
        raise AssertionError(
            f"{leg.name} --list failed ({out.returncode}): {out.stderr.strip()}")
    return {line.strip() for line in out.stdout.splitlines() if line.strip()}


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
    def test_every_python_file_is_syntax_checked_by_the_python_leg(self) -> None:
        """The claim is "every python file in the repo"; the leg must cover
        all of them, modules included."""
        py = _python_files()
        self.assertGreater(len(py), 100, "the sweep should find the repo's python files")
        rows = _rows()
        covering = [name for name, cmd in rows if "python-syntax.sh" in cmd]
        self.assertTrue(covering,
                        "no row runs the derived python-syntax leg: "
                        f"{len(py)} python files would be unchecked")
        members = _leg_members(PYTHON_LEG)
        missing = [rel for rel in py if rel not in members]
        self.assertEqual(missing, [],
                         "python files the python-syntax leg does not check: "
                         f"{missing}")

    def test_every_shell_script_is_syntax_checked_by_the_shell_leg(self) -> None:
        """The claim is "every shell script in the repo" — 32 files, not the
        11 the row used to name."""
        shells = _shell_files()
        self.assertGreater(len(shells), 20, "the sweep should find the repo's shell scripts")
        rows = _rows()
        covering = [name for name, cmd in rows if "shell-syntax.sh" in cmd]
        self.assertTrue(covering,
                        "no row runs the derived shell-syntax leg: "
                        f"{len(shells)} shell scripts would be unchecked")
        members = _leg_members(SHELL_LEG)
        missing = [rel for rel in shells if rel not in members]
        self.assertEqual(missing, [],
                         "shell scripts the shell-syntax leg does not check: "
                         f"{missing}")

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

    def test_the_two_legs_do_not_cover_each_other(self) -> None:
        """A file in both legs is checked by the wrong parser: `bash -n` on a
        python file reports errors that are not there."""
        py = _leg_members(PYTHON_LEG)
        sh = _leg_members(SHELL_LEG)
        self.assertEqual(sorted(py & sh), [],
                         "files claimed by both the python and the shell leg")


if __name__ == "__main__":
    unittest.main(verbosity=2)
