"""The verification map must name files that exist, and suites that run.

Round 127. `scripts/verify-map.tsv` is the table that says which suite covers
which changed file. Two of its rows named files that had been split away in
round 108 - `test_protocol_messaging.py` and `test_protocol_liveness.py` - so
the map claimed coverage for two files that could not run. Nothing broke,
because a file that does not exist never appears in a diff; but a reader
auditing coverage counts two suites that cannot run.

The map is also the only place that names suite names, and
`scripts/verify-changed.sh` looks each name up in the profile to find the
command to run. A name that is in the map but not in the profile is a suite
that cannot be launched - the changed-file gate would report "suite X missing
from the profile" and skip it.

So the map is checked against the tree and against the profile:
  1. every path the map names exists (the two phantom rows fail this);
  2. every suite the map names exists in the profile (a stale suite name
     fails this);
  3. every path the map names is tracked, so the map cannot describe a file
     that only exists in one worker's tree.

Run: python3 mcp/pair_mailbox/test_verify_map.py
"""
from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
MAP = ROOT / "scripts" / "verify-map.tsv"
PROFILE = ROOT / "scripts" / "verification-profile.tsv"


def _tracked() -> set[str]:
    out = subprocess.run(["git", "ls-files"], cwd=ROOT,
                         capture_output=True, text=True).stdout or ""
    return {line.strip() for line in out.splitlines() if line.strip()}


def _map_rows() -> list[tuple[str, str]]:
    """(path, suite) for every mapping row.

    The `follow:<file>` rows are cross-suite pins, not paths, and the comment
    lines are not rows at all.
    """
    rows = []
    for raw in MAP.read_text(encoding="utf-8").splitlines():
        if raw.startswith("#") or not raw.strip():
            continue
        parts = raw.split("\t")
        if len(parts) >= 2:
            rows.append((parts[0].strip(), parts[1].strip()))
    return rows


def _profile_suites() -> set[str]:
    suites = set()
    for raw in PROFILE.read_text(encoding="utf-8").splitlines():
        if raw.startswith("#") or not raw.strip():
            continue
        parts = raw.split("\t")
        if parts:
            suites.add(parts[0].strip())
    return suites


# The pseudo-suites verify-changed.sh resolves without a profile row: a name
# ending in `-check` means "no separate suite, this is covered by static", and
# `installer-subset` runs a fast installer pass. Both are handled by the
# `case` in scripts/verify-changed.sh:51, so a map row may name them.
PSEUDO_SUITES = {"installer-subset"}


def _is_pseudo(suite: str) -> bool:
    return suite in PSEUDO_SUITES or suite.endswith("-check")


class TestVerifyMap(unittest.TestCase):
    def test_every_path_the_map_names_exists(self) -> None:
        """The claim: the map maps files. A path it names must be a file.

        A trailing slash is a directory prefix, which the map matches by
        prefix (`index($f, $1"/") == 1` in verify-changed.sh) — so the check
        is that the directory holds tracked files, not that it is itself a
        tracked path.
        """
        tracked = _tracked()
        missing = []
        for path, suite in _map_rows():
            if path.startswith("follow:"):
                continue
            if path.endswith("/"):
                prefix = path
                if not any(f.startswith(prefix) for f in tracked):
                    missing.append((path, suite))
            elif path not in tracked:
                missing.append((path, suite))
        self.assertEqual(missing, [],
                         "verify-map.tsv names files that are not tracked: "
                         f"{missing}")

    def test_every_suite_the_map_names_exists_in_the_profile(self) -> None:
        """A suite name in the map is a lookup key into the profile: it is how
        verify-changed.sh finds the command to run. A name that is neither in
        the profile nor a pseudo-suite is a suite that cannot be launched."""
        suites = _profile_suites()
        unknown = sorted({suite for _, suite in _map_rows()
                          if suite not in suites and not _is_pseudo(suite)})
        self.assertEqual(unknown, [],
                         "verify-map.tsv names suites the profile does not "
                         f"define: {unknown}")

    def test_the_map_is_not_empty(self) -> None:
        """A map that names nothing would pass the two checks above for the
        wrong reason."""
        rows = [r for r in _map_rows() if not r[0].startswith("follow:")]
        self.assertGreater(len(rows), 50,
                           "the map should carry the repo's file-to-suite table")


class TestVerifyChangedRouting(unittest.TestCase):
    """The changed-file gate must SELECT what the map promises.

    Round 129 found the gate's own routing rule broken, and silently: the
    awk compared `$f` (FIELD f) instead of the variable `f`, and looked for
    `index($f, $1"/")` — a directory row already ends in a slash, so the
    probe was "skills//". Both mistakes made every DIRECTORY row of the map
    dead, so a change under `skills/` selected no suite at all: the gate ran
    static and nothing else, and it said so with an empty suite list.

    The rule is checked here against the real script, not against a copy of
    the rule: a test that re-implements the selector cannot see the selector
    break.
    """

    SCRIPT = ROOT / "scripts" / "verify-changed.sh"

    def _select(self, path: str) -> str:
        """The suite verify-changed.sh picks for a changed path.

        The real script, asked directly — a test that re-implements the
        selector cannot see the selector break.
        """
        out = subprocess.run(
            ["bash", str(self.SCRIPT), "--suite-for", path], cwd=ROOT,
            capture_output=True, text=True, timeout=60)
        return out.stdout.strip()

    def test_a_file_under_a_mapped_directory_reaches_that_suite(self) -> None:
        """The directory rows are the ones the broken rule killed."""
        for prefix, suite in (("skills/", "waiting-contract"),
                              (".agents/", "waiting-contract"),
                              (".opencode/skills/", "waiting-contract")):
            with self.subTest(prefix=prefix):
                probe = f"{prefix}pair-work/SKILL.md"
                self.assertIn(suite, self._select(probe),
                              f"{probe} must reach the {suite} suite — the "
                              f"map's {prefix} row promises it")

    def test_an_exact_row_still_wins_over_a_directory_row(self) -> None:
        """scripts/verify-map.tsv is under scripts/ AND has its own row.

        The exact row must win, or the file's own suite would be lost to the
        directory prefix that also covers it.
        """
        selected = self._select("scripts/verify-map.tsv")
        self.assertIn("verify-map", selected,
                      f"an exact row must beat the directory prefix: got {selected!r}")
        self.assertNotIn("scripts-check", selected,
                         f"the directory prefix must not win: got {selected!r}")

    def test_the_offline_supervisor_row_runs_with_the_file_it_covers(self) -> None:
        """Round 137: the new supervisor-offline row was in no fast gate.

        The full gate runs it, but the per-commit gate (verify-changed.sh) runs
        only the suites the map names for the changed paths - and the map named
        `supervisor` alone, because a path selects ONE suite (the longest
        matching row wins). The second environment therefore reached the full
        gate only: a change to the suite could break the offline row, and no
        fast gate would have said so.
        """
        selected = self._select("bin/pairpilot_test.py")
        self.assertIn("supervisor", selected,
                      f"the suite's own row must still run: got {selected!r}")
        self.assertIn("supervisor-offline", selected,
                      f"the offline supervisor row must run with its file: "
                      f"got {selected!r}")

    def test_a_narrower_directory_row_beats_a_wider_one(self) -> None:
        """skills/ holds several independent subsystems, so it needs a row
        narrower than itself.

        Round 133: skills/ was mapped to waiting-contract for the whole
        tree, so the session-finalizer subsystem - a complete package with
        its own 70-test suite - was covered by a suite about the mailbox
        waiting contract, and its own tests ran in no gate at all. The
        narrower row must win, and the answer must not depend on the order
        somebody typed the rows in.
        """
        selected = self._select("skills/session-finalizer/scripts/finalize-check")
        self.assertIn("session-finalizer", selected,
                      f"the finalizer's own suite must cover it: got {selected!r}")
        self.assertNotIn("waiting-contract", selected,
                         f"the wider skills/ row must not win: got {selected!r}")
        other = self._select("skills/pair-work/SKILL.md")
        self.assertIn("waiting-contract", other,
                      f"the other skills keep their own suite: got {other!r}")
        self.assertNotIn("session-finalizer", other,
                         f"the narrow row must not reach outside its tree: got {other!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
