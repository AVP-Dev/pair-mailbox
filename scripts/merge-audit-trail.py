#!/usr/bin/env python3
"""Audit the merge trail: every merged PR must have a pr_merged event.

Why: the journal is the only record that a merge went through the sanctioned
path (`bin/pairctl pr merge`, which writes the event). Nothing compared the
PRs merged on the remote with the journal's events, so a merge done outside
the path left no trace at all. This check makes that hole visible.

The debt is never healed by writing events into the past: an event for a merge
that happened long ago would be a forgery. The baseline file names the
absences instead, and the check fails only on a NEW absence.

Fail closed: when the merged PRs cannot be read (no gh, no auth, no answer) or
the journal is not where it should be, the check fails. It never reports
success because it could not look.

Runtime: stdlib. The inputs are the remote (through gh), the pair's state
directory and the baseline file.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BASELINE = REPO_ROOT / "scripts" / "merge-audit-baseline.tsv"
DEFAULT_JOURNAL = REPO_ROOT / ".pair"
GH_LIMIT = 500
GH_TIMEOUT = 60
# The journal records a merge in three ways: the sanctioned path writes
# `pr_merged`, a repair of a merge that happened outside the gate writes
# `pr_merge_repaired` (PROTOCOL 10g.6), and a forced merge writes
# `pr_merge_forced` (10g.5). All three mean the same thing to this check:
# the journal knows the merge happened. A fourth kind - the merge itself
# happening outside the path - leaves nothing, and that is what the check
# exists to catch.
RECORDED_KINDS = ("pr_merged", "pr_merge_repaired", "pr_merge_forced")


class AuditError(RuntimeError):
    """The audit could not read what it needs; the answer is a failure, not a pass."""


def merged_pr_numbers(repo_dir: Path, limit: int = GH_LIMIT) -> set[int]:
    """The PR numbers merged on the remote. AuditError when GitHub cannot be read."""
    try:
        proc = subprocess.run(
            ["gh", "pr", "list", "--state", "merged", "--limit", str(limit),
             "--json", "number"],
            cwd=str(repo_dir), capture_output=True, text=True, timeout=GH_TIMEOUT)
    except FileNotFoundError as exc:
        raise AuditError("gh is not installed: the merged PRs cannot be read") from exc
    except subprocess.TimeoutExpired as exc:
        raise AuditError(f"gh did not answer in {GH_TIMEOUT}s: the merged PRs cannot be read") from exc
    if proc.returncode != 0:
        lines = (proc.stderr or "").strip().splitlines()
        detail = lines[-1] if lines else f"exit {proc.returncode}"
        raise AuditError(f"gh could not list the merged PRs: {detail}")
    try:
        rows = json.loads(proc.stdout or "[]")
    except ValueError as exc:
        raise AuditError("gh answered with something that is not JSON: "
                         "the merged PRs cannot be read") from exc
    numbers: set[int] = set()
    for row in rows:
        try:
            numbers.add(int(row["number"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise AuditError(f"gh answered with a row without a PR number: {row!r}") from exc
    return numbers


def journal_files(journal_dir: Path) -> list[Path]:
    """The active journal and every rotated archive. The rotation holds the older events."""
    journal_dir = Path(journal_dir)
    if not journal_dir.is_dir():
        raise AuditError(f"no journal at {journal_dir}: the audit trail lives in the "
                         "pair's state directory, not in the repo")
    files = [journal_dir / "transcript.jsonl"]
    files += sorted((journal_dir / "transcript-archive").glob("*.jsonl"))
    present = [path for path in files if path.is_file()]
    if not present:
        raise AuditError(f"no transcript under {journal_dir}: the audit trail is empty")
    return present


def journal_pr_merged(journal_dir: Path) -> set[int]:
    """The PR numbers the journal holds a merge record for, in the active file
    and in the rotated archives."""
    recorded: set[int] = set()
    for path in journal_files(journal_dir):
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("kind") not in RECORDED_KINDS:
                    continue
                try:
                    recorded.add(int(row["pr_id"]))
                except (KeyError, TypeError, ValueError):
                    continue
    return recorded


def read_baseline(path: Path) -> dict[int, str]:
    """The known debt: PR number -> why it has no event."""
    path = Path(path)
    if not path.is_file():
        raise AuditError(f"no baseline at {path}: the known debt must be named")
    known: dict[int, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        try:
            number = int(parts[0])
        except ValueError as exc:
            raise AuditError(f"the baseline's first column must be a PR number: {line!r}") from exc
        known[number] = parts[1] if len(parts) > 1 else ""
    return known


def audit(merged: set[int], recorded: set[int],
          known: dict[int, str]) -> tuple[list[int], list[int], list[int]]:
    """The escapes (merged without an event), the new ones, and the baseline entries
    that are no longer escapes."""
    escapes = sorted(merged - recorded)
    new_escapes = [number for number in escapes if number not in known]
    repaid = sorted(number for number in known if number not in escapes)
    return escapes, new_escapes, repaid


def render(merged: set[int], recorded: set[int], escapes: list[int],
           new_escapes: list[int], repaid: list[int], baseline: Path) -> str:
    """The report: the debt is a number, not a hole."""
    lines = [f"merge-audit: {len(merged)} merged PRs, "
             f"{len(merged & recorded)} with a merge record, "
             f"{len(escapes)} without",
             f"merge-audit: known debt: {len(escapes) - len(new_escapes)} "
             f"(named in {baseline})"]
    if repaid:
        lines.append("merge-audit: WARNING: the baseline names PRs that are no longer "
                     f"escapes: {', '.join(str(number) for number in repaid)} - "
                     "update the baseline")
    if new_escapes:
        lines.append(f"merge-audit: FAILED: {len(new_escapes)} merges escaped the "
                     f"sanctioned path: {', '.join(str(number) for number in new_escapes)}")
        return "\n".join(lines)
    lines.append("merge-audit: PASS (no merge outside the sanctioned path)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit the merge trail: the merged PRs against the journal's "
                    "pr_merged events")
    parser.add_argument("--repo", default=str(REPO_ROOT),
                        help="the repository to ask GitHub about")
    parser.add_argument("--journal", default=str(DEFAULT_JOURNAL),
                        help="the pair's state directory (the journal and its archives)")
    parser.add_argument("--baseline", default=str(DEFAULT_BASELINE),
                        help="the file naming the known debt")
    args = parser.parse_args(argv)
    try:
        merged = merged_pr_numbers(Path(args.repo))
        recorded = journal_pr_merged(Path(args.journal))
        known = read_baseline(Path(args.baseline))
    except AuditError as exc:
        print(f"merge-audit: FAILED: {exc}")
        return 2
    escapes, new_escapes, repaid = audit(merged, recorded, known)
    print(render(merged, recorded, escapes, new_escapes, repaid, Path(args.baseline)))
    return 1 if new_escapes else 0


if __name__ == "__main__":
    sys.exit(main())
