#!/usr/bin/env python3
"""branch-class-check: are remote branch names legal under PROTOCOL §20?

The class of a branch must be readable from its name (§20 rule 1): GitHub
branch protection and rulesets can only match names, so a branch outside the
known patterns is a branch no rule can protect. This script is the origin-side
guard for that contract.

Fail-closed in every direction:

- no git, no origin refs, or a `git` that fails mid-run  -> refuse, do not pass
  on silence. An empty branch list that is not *proven* empty is a failure.
- an unrecognised name        -> ANOMALY (refused)
- an accumulator name (dev, develop, integration, staging) -> ANOMALY, because
  §20 rule 4 names them an antipattern, not a preference
- `master` present and legal  -> reported, never a failure; master is the trunk

Local refs only: no `gh`, no network. Reachability/merge state is deliberately
NOT decided here — that is the janitor's job and it needs `gh` (§20 janitor rule
1). This check answers one question: do the names conform.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# §20 class table lives in one place, shared with the janitor
# (mcp/pair_mailbox/branch_classes.py): two copies of the table would drift, and
# the drift would let the janitor delete what this checker calls legal.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mcp" / "pair_mailbox"))
from branch_classes import ACCUMULATORS, TRUNK, classify  # noqa: E402


def remote_branches(root: Path) -> list[str]:
    """Remote branch short names, or raise: never an empty list by accident.

    A `git branch -r` that fails answers empty on stdout, and an empty list
    reads as "nothing to check" — a PASS. That is the one direction this guard
    cannot afford (compare review.py: the reachability probe says `unknown`,
    never `ok`, when git misbehaves).
    """
    proc = subprocess.run(
        ["git", "branch", "-r", "--format=%(refname:short)"],
        cwd=str(root), capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"git branch -r failed (rc={proc.returncode}); refusing to judge "
            f"branch names without the real list: {proc.stderr.strip()[:200]}"
        )
    names = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        # `git branch -r` prints `origin/HEAD -> origin/master` on some setups,
        # and a bare `origin` line for the remote itself — neither is a branch,
        # and neither has a class.
        if not line or "->" in line:
            continue
        if "/" not in line:
            continue
        names.append(line.split("/", 1)[1])
    return names


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", default=".", help="repository root")
    parser.add_argument("--fail-on-anomaly", action="store_true",
                        help="exit non-zero when a branch name is an anomaly")
    args = parser.parse_args(argv)

    root = Path(args.repo).resolve()
    try:
        names = remote_branches(root)
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        print(f"branch-class-check: refusing to check: {exc}", file=sys.stderr)
        return 2

    if not names:
        # Proven empty by a successful git run. Legal, and worth saying out loud.
        print("branch-class-check: no remote branches")

    rows: list[tuple[str, str]] = []
    for name in sorted(names):
        rows.append((name, classify(name)))

    anomalies = [n for n, c in rows if c == "anomaly"]
    for name in sorted(ACCUMULATORS):
        if name in anomalies:
            print(f"branch-class-check: ANOMALY {name} — accumulators are an "
                  f"antipattern under §20 rule 4")
    for name in anomalies:
        if name not in ACCUMULATORS:
            print(f"branch-class-check: ANOMALY {name} — no §20 class matches "
                  f"this name, so no rule can protect it")

    counts = {"trunk": 0, "strategic": 0, "ephemeral": 0, "personal": 0,
              "anomaly": 0}
    for _, c in rows:
        counts[c] += 1
    for name, c in rows:
        if c != "anomaly":
            print(f"  {c:11s} {name}")
    print(f"branch-class-check: {len(rows)} branches — "
          f"{counts['trunk']} trunk, {counts['strategic']} strategic, "
          f"{counts['ephemeral']} ephemeral, {counts['personal']} personal, "
          f"{counts['anomaly']} anomaly")

    if anomalies and args.fail_on_anomaly:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
