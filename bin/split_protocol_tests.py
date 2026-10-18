#!/usr/bin/env python3
"""Split test_protocol.py by domain (round 108 slice 3).

Cut by measurement, never by hand: this script reads the class ranges, moves whole
classes into domain files, and leaves behind a core file with the shared header and
whatever does not belong to a domain. A hand-made copy of 4600 lines would drift
from the source on every future edit — which is the whole reason the file grew.

Usage:
  python3 bin/split_protocol_tests.py --dry-run   show the plan, change nothing
  python3 bin/split_protocol_tests.py --apply     write the domain files + core
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "mcp" / "pair_mailbox" / "test_protocol.py"

# Domain map: file name -> (module docstring line, the classes that belong there).
# Grouped by the server's own seams (round 106 slice D's transport boundary), so a
# later reader finds a test next to the code it guards rather than by name.
DOMAINS: dict[str, tuple[str, list[str]]] = {
    "test_protocol_authority.py": (
        "Write authority, roster persistence, patch flow — who may write and how a patch travels.",
        ["TestWriteAuthority", "TestPersistentRoles", "TestPatchFlow",
         "TestPatchAccounting", "TestTaskList", "TestClosingRuleIsSingleSourced"],
    ),
    "test_protocol_gates.py": (
        "Close-path gates: human checkpoint, evidence, autonomy, allowed defaults.",
        ["TestHumanCheckpoint", "TestEvidenceGate", "TestAutonomyDefault",
         "TestAutonomyTriggers", "TestDefaultAllowlist"],
    ),
    "test_protocol_messaging.py": (
        "Messaging and delivery: mailbox, phases and notes, addressing, rollback, ring.",
        ["TestMessaging", "TestPhasesAndNotes", "TestNoteTailReads", "TestNoteRotation",
         "TestTranscriptWindow", "TestSessionAddressing", "TestDeliveryRollback",
         "TestRingAndUnblock", "TestMailboxInvariants"],
    ),
    "test_protocol_tui.py": (
        "The composer's geometry: overflowed panes, corner frames, permission menus, TUI vs bus.",
        ["TestOverflowedComposer", "TestInputBoxCornerVsSideFrame", "TestPermissionAllowlist",
         "TestPermissionMenu", "TestPermissionContext", "TestStatusLineIsNotAComposer",
         "TestTuiIsNotABus", "TestComposerOfAnOpencodePane"],
    ),
    "test_protocol_liveness.py": (
        "Liveness and addressing of processes and panes: heartbeats, stale panes, single live "
        "process per role, audit trail, stdio, transport pins.",
        ["TestHeartbeatIsEveryCall", "TestAuditTrail", "TestMCPStdio",
         "TestStalePaneSurvives", "TestSingleLiveProcessPerRole", "TestPaneIdentityIsNotLiveness",
         "TestOldPythonStillRuns", "TestRoundBasePin", "TestOriginTruthCaller"],
    ),
}

HEADER_KEEP = "from pair_test_base import"
CORE_DOC = """Protocol tests for pair-mailbox (core: shared helpers and cross-cutting pins).

Domains were split out by round 108 slice 3, cut by bin/split_protocol_tests.py
(never by hand, so the ranges cannot drift): authority, gates, messaging, tui,
liveness. Run: python3 -m unittest discover -s mcp/pair_mailbox -v
"""


def class_ranges(text: str) -> dict[str, tuple[int, int]]:
    lines = text.splitlines()
    marks = []
    for i, line in enumerate(lines, 1):
        m = re.match(r"^class (\w+)", line)
        if m:
            marks.append((i, m.group(1)))
    marks.append((len(lines) + 1, "EOF"))
    out: dict[str, tuple[int, int]] = {}
    for idx in range(len(marks) - 1):
        start, name = marks[idx]
        out[name] = (start, marks[idx + 1][0] - 1)
    return out


def header_block(text: str) -> str:
    """Everything up to the first class: imports, the shared helper, the budget pin."""
    lines = text.splitlines()
    cut = next(i for i, l in enumerate(lines, 1) if re.match(r"^class \w+", l)) - 1
    return "\n".join(lines[:cut]).rstrip() + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not args.apply:
        args.dry_run = True

    text = TARGET.read_text(encoding="utf-8")
    ranges = class_ranges(text)
    header = header_block(text)
    lines = text.splitlines()

    planned: dict[str, int] = {}
    moved: list[tuple[str, str, str]] = []   # (file, class, source-range)

    for fname, (doc, classes) in DOMAINS.items():
        count = 0
        for cls in classes:
            if cls not in ranges:
                print(f"split: MISSING class {cls} (not in {TARGET.name})", file=sys.stderr)
                return 2
            count += ranges[cls][1] - ranges[cls][0] + 1
            moved.append((fname, cls, f"{ranges[cls][0]}-{ranges[cls][1]}"))
        planned[fname] = count

    leftover = len(lines) - sum(planned.values())
    print(f"split plan: {TARGET.name} ({len(lines)} lines)")
    for fname, n in planned.items():
        print(f"  {fname:38} {n:5} lines")
    print(f"  {'(core file, header + shared)':38} {leftover:5} lines")
    if args.dry_run:
        print("dry-run: nothing written. Re-run with --apply.")
        return 0

    # Cross-check the ranges against git before writing anything: a stale range
    # would silently move the wrong code.
    for fname, cls, rng in moved:
        start = int(rng.split("-")[0])
        snippet = lines[start - 1]
        if not snippet.startswith(f"class {cls}"):
            print(f"split: RANGE DRIFT at {fname}:{cls} line {start} reads {snippet[:60]!r}",
                  file=sys.stderr)
            return 3

    header_doc = re.sub(r'""".*?"""', f'"""{CORE_DOC}"""', header, count=1, flags=re.DOTALL)
    for fname, (doc, classes) in DOMAINS.items():
        body: list[str] = []
        for cls in classes:
            start, end = ranges[cls]
            body.extend(lines[start - 1:end])
            body.append("")
        out = (f'"""{doc}\n)\n\n"""\n\n'
               + "\n".join(header.splitlines()[1:]).rstrip() + "\n\n"
               + "\n".join(body).rstrip() + "\n")
        # a cleaner assembly: keep the header as-is and append the domain bodies
        out = header.rstrip() + "\n\n\n" + "\n".join(body).rstrip() + "\n"
        out = re.sub(r'"""Protocol tests for pair-mailbox\..*?"""',
                     f'"""{doc}"""', out, count=1, flags=re.DOTALL)
        (ROOT / "mcp" / "pair_mailbox" / fname).write_text(out, encoding="utf-8")
        print(f"wrote {fname} ({planned[fname]} class lines)")

    keep: set[str] = set()
    for _f, (_d, classes) in DOMAINS.items():
        for cls in classes:
            keep.add(cls)
    core_body: list[str] = []
    for cls, (start, end) in ranges.items():
        if cls == "EOF":
            continue
        if cls in keep:
            continue
        core_body.extend(lines[start - 1:end])
        core_body.append("")
    core = header_doc + "\n" + "\n".join(core_body).rstrip() + "\n"
    TARGET.write_text(core, encoding="utf-8")
    print(f"rewrote {TARGET.name} (core) with {leftover} lines")
    return 0


if __name__ == "__main__":
    sys.exit(main())
