#!/usr/bin/env python3
"""Check that the Russian protocol mirror has the original's structure.

Why: `docs/PROTOCOL.ru.md` is a deliberate MIRROR of `docs/PROTOCOL.md`, and a
mirror drifts. The drift was invisible because nothing compared the two: the
mirror lacked §9b, §17 and the §6 plan-gate subsection, and nobody learned of
it until round 150 measured the gap.

What is checked is STRUCTURE, not prose: every numbered section and every
subsection of the original must exist in the mirror, under the same number, in
the same order. The prose is a translation and cannot be compared mechanically;
the skeleton can, and the skeleton is what a reader navigates by.

The section NUMBERS are not renumbered when one is missing — §14-16 are absent
from BOTH files, a numbering gap in the original, and renumbering the mirror
would give it a structure the original does not have.

Usage: protocol_parity_check.py [--original PATH] [--mirror PATH]
Exit: 0 when the structures match, 1 when they do not, 2 when a file is
unreadable (that is a failure too: a check that cannot look proves nothing).
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ORIGINAL = REPO_ROOT / "docs" / "PROTOCOL.md"
DEFAULT_MIRROR = REPO_ROOT / "docs" / "PROTOCOL.ru.md"

# A numbered section: `## 9b. Title` or `## 17. Title`.
SECTION = re.compile(r"^##\s+(\S+)\.\s+(.*)$")
# A subsection: `### Title` under the section that precedes it.
SUBSECTION = re.compile(r"^###\s+(.*)$")


def parse_structure(text: str) -> tuple[list[str], list[tuple[str, str]]]:
    """The skeleton: the numbered sections in order, and (section, title) pairs
    for the subsections."""
    sections: list[str] = []
    subsections: list[tuple[str, str]] = []
    current: str | None = None
    for line in text.splitlines():
        match = SECTION.match(line)
        if match:
            current = match.group(1)
            sections.append(current)
            continue
        match = SUBSECTION.match(line)
        if match and current is not None:
            subsections.append((current, match.group(1).strip()))
    return sections, subsections


def compare(original: str, mirror: str) -> list[str]:
    """The differences between the two skeletons, as sentences."""
    original_sections, original_subs = parse_structure(original)
    mirror_sections, mirror_subs = parse_structure(mirror)
    problems: list[str] = []

    missing = [number for number in original_sections if number not in mirror_sections]
    if missing:
        problems.append("the mirror is missing section(s): " + ", ".join(missing))
    extra = [number for number in mirror_sections if number not in original_sections]
    if extra:
        problems.append("the mirror has section(s) the original does not: "
                        + ", ".join(extra))
    if not missing and not extra and original_sections != mirror_sections:
        problems.append("the sections are in a different order: the original has "
                        + ", ".join(original_sections) + " and the mirror has "
                        + ", ".join(mirror_sections))

    original_numbers = [number for number, _ in original_subs]
    mirror_numbers = [number for number, _ in mirror_subs]
    original_counts = {number: original_numbers.count(number) for number in set(original_numbers)}
    mirror_counts = {number: mirror_numbers.count(number) for number in set(mirror_numbers)}
    for number in sorted(set(original_counts) | set(mirror_counts)):
        want = original_counts.get(number, 0)
        have = mirror_counts.get(number, 0)
        if want == have:
            continue
        # The titles are named from the ORIGINAL: the mirror's titles are a
        # translation and will not match them word for word, so the count is
        # what is compared, and the original's titles are what the reader is
        # told is missing.
        titles = [title for owner, title in original_subs if owner == number]
        problems.append(
            f"§{number} has {want} subsection(s) in the original and {have} "
            "in the mirror; the original's are: " + "; ".join(titles))
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check that the Russian protocol mirror has the original's "
                    "section and subsection structure")
    parser.add_argument("--original", default=str(DEFAULT_ORIGINAL),
                        help="the protocol original")
    parser.add_argument("--mirror", default=str(DEFAULT_MIRROR),
                        help="the Russian mirror")
    args = parser.parse_args(argv)
    try:
        original = Path(args.original).read_text(encoding="utf-8")
        mirror = Path(args.mirror).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"protocol-parity: FAILED: cannot read the protocol files: {exc}",
              file=sys.stderr)
        return 2
    problems = compare(original, mirror)
    if problems:
        print("protocol-parity: FAILED: the mirror does not match the original:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    sections, subsections = parse_structure(original)
    print(f"protocol-parity: PASS: {len(sections)} sections and "
          f"{len(subsections)} subsections match")
    return 0


if __name__ == "__main__":
    sys.exit(main())
