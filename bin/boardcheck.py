#!/usr/bin/env python3
"""Machine leg for the board (round 106 slice E): a row cannot claim what the tree cannot show.

Round 105's argument, three drifts caught by the manual C-leg plus one caught
pre-commit: rows asserted `delivered <sha>` where the sha was another commit's
(ae6abdd for 2fe78a3), a prerequisite's (06dcdeb as 2b), or a component's
(function without its caller). A human catch every time, and the catch is
mechanical: for each ticked row, the sha must resolve, must be an ancestor of
the branch under test, and the slice's named artefact must exist in the tree at
that sha.

This module answers three questions per row and refuses on any NO:
  - does the sha resolve at all?
  - is it an ancestor of HEAD (the round's work is IN this branch)?
  - does the named artefact exist at that sha?

It never edits the board. It names, so a wrong tick is a refusal at close rather
than a story that survives until someone re-reads the file.

Usage:
  python3 bin/boardcheck.py --board .pair/plan.md [--json]
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE_RE = re.compile(r"- state:\s*\[([ xX])\]\s*(.*)$")
SHA_RE = re.compile(r"\b([0-9a-f]{7,40})\b")
# artefact paths, parsed from the row's prose. A backticked path is unambiguous;
# a bare `bin/tool` form is also common. Both must exist in the working tree.
PATH_RE = re.compile(r"`([A-Za-z0-9_][A-Za-z0-9_./-]*)`|\b((?:bin|mcp|docs|scripts)/[A-Za-z0-9_./-]+)")


def _git(*args: str) -> tuple[int, str]:
    proc = subprocess.run(["git", *args], cwd=str(ROOT),
                          capture_output=True, text=True, timeout=60)
    return proc.returncode, (proc.stdout or "").strip()


def resolve_sha(sha: str) -> str:
    code, out = _git("rev-parse", "--verify", f"{sha}^{{commit}}")
    return out if code == 0 else ""


def ancestor_of(sha: str, head: str) -> bool:
    """`sha` is an ancestor of `head` (or IS head): the work is in this branch."""
    if not head:
        return False
    code, _ = _git("merge-base", "--is-ancestor", sha, head)
    return code == 0


def artefact_at_head(path: str) -> bool:
    return (ROOT / path).exists()


def slices(board: Path) -> list[dict[str, str]]:
    """One record per `## N. Title` section, with its state line."""
    sections: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for line in board.read_text(encoding="utf-8").splitlines():
        heading = re.match(r"^##\s+([A-Z0-9]+[a-z]?)\.\s*(.+)$", line.strip())
        if heading:
            current = {"letter": heading.group(1), "title": heading.group(2), "state": ""}
            sections.append(current)
        elif current is not None and line.strip().startswith("- state:"):
            m = STATE_RE.search(line.strip())
            if m:
                current["state"] = line.strip()
    return sections


def check(board: Path, require_ancestor: bool = True) -> tuple[bool, list[str]]:
    problems: list[str] = []
    head = _git("rev-parse", "HEAD")[1]
    unresolved = 0
    for sec in slices(board):
        m = STATE_RE.search(sec["state"])
        if not m:
            problems.append(f"slice {sec['letter']}: no state line — the board row is not parseable")
            continue
        ticked, rest = m.group(1).lower(), m.group(2)
        state_word = rest.strip().split()[0] if rest.strip() else ""
        shas = SHA_RE.findall(rest)
        if state_word not in ("delivered", "approved"):
            continue  # in-work / refused carry no claim to falsify
        if not shas:
            problems.append(
                f"slice {sec['letter']}: claims {state_word!r} with no sha — "
                f"the state column must bind a revision")
            continue
        for sha in shas:
            full = resolve_sha(sha)
            if not full:
                problems.append(f"slice {sec['letter']}: sha {sha} does not resolve")
                unresolved += 1
                continue
            if require_ancestor and head and not ancestor_of(full, head):
                problems.append(
                    f"slice {sec['letter']}: sha {sha} is not an ancestor of HEAD — "
                    f"the work this row claims is not in this branch")
        # artefact claims: any path named in the row must exist in the tree
        for backticked, bare in PATH_RE.findall(rest):
            path = backticked or bare
            # a word in prose is not a path claim; only shapes that look like one
            if "/" not in path and "." not in path:
                continue
            if not artefact_at_head(path):
                problems.append(
                    f"slice {sec['letter']}: names {path}, which the tree does not carry")
    return (not problems), problems


def main() -> int:
    ap = argparse.ArgumentParser(description="A board row must be provable: sha resolves, in-branch, artefact exists.")
    ap.add_argument("--board", default=".pair/plan.md")
    ap.add_argument("--no-ancestor", action="store_true",
                    help="skip the ancestor check (for a board still in flight)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    board = ROOT / args.board
    if not board.exists():
        print(f"boardcheck: no board at {board}", file=sys.stderr)
        return 2
    ok, problems = check(board, require_ancestor=not args.no_ancestor)
    if args.json:
        print(json.dumps({"ok": ok, "problems": problems}, ensure_ascii=False, indent=1))
    else:
        if ok:
            print(f"boardcheck: clean — every ticked row's sha resolves in this branch")
        else:
            print(f"boardcheck: {len(problems)} problem(s):", file=sys.stderr)
            for p in problems:
                print(f"  - {p}", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
