#!/usr/bin/env python3
"""backlog-check: does docs/backlog/ hold only open debt, as PROTOCOL says?

The rule lives in three places and nowhere else: PROTOCOL.md:551,
PROTOCOL.ru.md:531 and skills/pair-backlog/SKILL.md:36 all say the same thing
— a card in docs/backlog/ is open debt, and a card that is closed is DELETED
with the closure recorded in the commit message, because the git history keeps
the record and the directory keeps only the debt.

Nothing checked that. The directory held five cards with `status: fixed` and
two whose frontmatter was incomplete, and every one of them read as a promise
the repository did not keep. This gate is that check, and it refuses rather
than repairs: a gate that edits the tree is a gate nobody can trust about
what it found.

Exit codes: 0 clean, 1 the directory breaks the rule, 2 the gate could not
look (missing directory, git failed) — never 0 for "nothing to look at".
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# The five fields PROTOCOL names for a card. Exactly five: the set is small
# enough to enumerate, and an extra field is how a card grows a second
# lifecycle (a `fixed_by` on a card that was never deleted is a closed card
# pretending to be open debt).
REQUIRED_FIELDS = ("title", "found", "severity", "round", "status")

SEVERITIES = ("critical", "high", "medium", "low")

# Both date shapes the repository uses: a bare day and a UTC instant.
FOUND_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}(:\d{2})?Z)?$")

# The directory's guide, not a card: a human browsing the folder on GitHub
# reads it first. It is the ONE exemption, named here rather than skipped by a
# glob — a glob skip would also swallow a card that happened to be called
# README.md, and the exemption would be invisible to the next reader.
NOT_A_CARD = "README.md"


class Problem:
    """One thing wrong with one file, named precisely enough to fix."""

    def __init__(self, path: Path, field: str, why: str) -> None:
        self.path = path
        self.field = field
        self.why = why

    def __str__(self) -> str:
        where = f"{self.path}: {self.field}" if self.field else f"{self.path}"
        return f"{where}: {self.why}"


def parse_frontmatter(path: Path) -> tuple[dict[str, str], list[Problem]]:
    """Read the card's frontmatter, or say why it is not a card."""
    problems: list[Problem] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return {}, [Problem(path, "", f"unreadable: {exc}")]

    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, [Problem(path, "", "no frontmatter: a card starts with a `---` line")]

    fields: dict[str, str] = {}
    end = None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end = index
            break
        if not line.strip():
            continue
        if ":" not in line:
            problems.append(Problem(path, f"line {index + 1}",
                                    f"not a frontmatter line: {line.strip()!r}"))
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if key in fields:
            problems.append(Problem(path, key, "the field is written twice"))
            continue
        fields[key] = value
    if end is None:
        problems.append(Problem(path, "", "the frontmatter never closes: no closing `---`"))
    return fields, problems


def check_fields(path: Path, fields: dict[str, str]) -> list[Problem]:
    """Every rule the card has to satisfy, each reported on its own."""
    problems: list[Problem] = []

    for key in REQUIRED_FIELDS:
        if key not in fields:
            problems.append(Problem(path, key, "the field is missing"))
        elif not fields[key]:
            problems.append(Problem(path, key, "the field is empty"))

    for key in sorted(set(fields) - set(REQUIRED_FIELDS)):
        problems.append(Problem(path, key, "not a field of a card: a card has exactly "
                                             + ", ".join(REQUIRED_FIELDS)))

    if fields.get("severity") and fields["severity"] not in SEVERITIES:
        problems.append(Problem(path, "severity",
                                f"{fields['severity']!r} is not one of "
                                + ", ".join(SEVERITIES)))

    if fields.get("found") and not FOUND_RE.match(fields["found"]):
        problems.append(Problem(path, "found",
                                f"{fields['found']!r} is not a date (YYYY-MM-DD, "
                                "or a UTC instant ending in Z)"))

    raw_round = fields.get("round")
    if raw_round and not re.fullmatch(r"\d+", raw_round):
        problems.append(Problem(path, "round", f"{raw_round!r} is not a number"))
    elif raw_round and int(raw_round) < 1:
        problems.append(Problem(path, "round", "the round is 0: a card was found in a round"))

    status = fields.get("status")
    if status and status != "open":
        problems.append(Problem(path, "status",
                                f"{status!r} is not open: a closed card is deleted, and the "
                                "commit message records what closed it"))
    return problems


def scan(root: Path) -> tuple[list[tuple[Path, dict[str, str]]], list[Problem]]:
    """Every card in the directory, and everything that breaks the rule."""
    directory = root / "docs" / "backlog"
    if not directory.is_dir():
        raise FileNotFoundError(f"{directory} is not there: the backlog directory "
                                "must exist, even when the debt is empty")

    cards: list[tuple[Path, dict[str, str]]] = []
    problems: list[Problem] = []
    for path in sorted(directory.iterdir()):
        if not path.is_file():
            problems.append(Problem(path, "", "not a file: the backlog holds cards, "
                                             "not directories"))
            continue
        if path.suffix != ".md":
            problems.append(Problem(path, "", "not a markdown file: the backlog holds cards"))
            continue
        if path.name == NOT_A_CARD:
            continue
        fields, found = parse_frontmatter(path)
        problems.extend(found)
        problems.extend(check_fields(path, fields))
        cards.append((path, fields))
    return cards, problems


def print_index(root: Path, cards: list[tuple[Path, dict[str, str]]]) -> None:
    """The open debt, counted and grouped, and the gap it leaves behind.

    Nothing is stored: a committed index is a second copy of the directory,
    and it is wrong the day a card is added.
    """
    by_severity: dict[str, list[str]] = {name: [] for name in SEVERITIES}
    for path, fields in cards:
        by_severity.setdefault(fields.get("severity", "?"), []).append(path.name)

    print(f"backlog-check: {len(cards)} open card(s) in docs/backlog/")
    for severity in SEVERITIES:
        names = by_severity.get(severity, [])
        print(f"  {severity:9s} {len(names):3d}  " + ", ".join(sorted(names)))

    # Sorted as numbers: round 11 is not after round 110, and a list that puts
    # it there is a list nobody reads twice.
    rounds = sorted({int(fields["round"]) for _, fields in cards
                     if fields.get("round", "").isdigit()})
    print("  rounds with open debt: " + ", ".join(str(r) for r in rounds))

    # A missing docs/rounds/ is not a reason to skip the gap: no round card can
    # exist in a directory that is not there, so every round with open debt is
    # in it. Reporting the gap needs no exception.
    rounds_dir = root / "docs" / "rounds"
    without = [r for r in rounds if not (rounds_dir / f"{r}.md").is_file()]
    if without:
        print("  rounds with open debt and no round card in docs/rounds/: "
              + ", ".join(str(r) for r in without))


def print_closed(root: Path, directory: Path) -> int:
    """The closed cards, read out of git: the archive the protocol names.

    Git is the archive (PROTOCOL.md:551 says so), so the archive is read from
    git rather than kept in a second directory that can disagree with it.
    """
    try:
        proc = subprocess.run(
            ["git", "log", "--diff-filter=D", "--name-only", "--format=C %ad",
             "--date=short", "--", str(directory)],
            cwd=str(root), capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"backlog-check: refusing to read the archive: {exc}", file=sys.stderr)
        return 2
    if proc.returncode != 0:
        print(f"backlog-check: refusing to read the archive: git failed "
              f"(rc={proc.returncode}): {proc.stderr.strip()[:200]}", file=sys.stderr)
        return 2

    day = ""
    closed: list[tuple[str, str]] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.startswith("C "):
            day = line[2:].strip()
        elif line.startswith("docs/backlog/") and line.endswith(".md"):
            closed.append((day, line.rsplit("/", 1)[1]))
    print(f"backlog-check: {len(closed)} closed card(s) in git history")
    for day, name in closed:
        print(f"  {day:10s} {name}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=".", help="repository root")
    parser.add_argument("--index", action="store_true",
                        help="print the open debt instead of checking it")
    parser.add_argument("--closed", action="store_true",
                        help="print the closed cards git history holds")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    # The archive lives in git, not in the directory, so reading it must not
    # depend on the directory still being there: git keeps a card after the
    # file is gone, and that is the whole point of the archive.
    if args.closed:
        return print_closed(root, root / "docs" / "backlog")

    try:
        cards, problems = scan(root)
    except FileNotFoundError as exc:
        print(f"backlog-check: refusing to check: {exc}", file=sys.stderr)
        return 2

    if args.index:
        print_index(root, cards)
        return 0

    for problem in problems:
        print(f"backlog-check: {problem}")
    if problems:
        print(f"backlog-check: {len(problems)} problem(s) — docs/backlog/ holds only "
              "open, well-formed cards", file=sys.stderr)
        return 1
    print(f"backlog-check: {len(cards)} card(s), all open and well-formed")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        # The reader went away (`| head`): the work was done, and a traceback
        # about a closed pipe would say nothing about the backlog.
        raise SystemExit(0)
