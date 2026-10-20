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


if __name__ == "__main__":
    unittest.main(verbosity=2)
