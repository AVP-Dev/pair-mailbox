"""Round summary: the machine-readable round digest for verify-ground-truth.

Why: the performer summary reconciles against physical facts, not performer
memory (lesson R81). The extractor only composes ready structures — the tree
facts, ledger.jsonl records, the anti-mock verdict — and invents nothing past them.
Library (build/render) + thin CLI.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import anti_mock_gate
import ledger
import session_facts

SUMMARY_VERSION = 1


def build(facts: dict[str, Any], records: list[dict[str, Any]],
          verdict: dict[str, Any]) -> dict[str, Any]:
    """Build the summary: input aggregation only, no new claims."""
    slices: dict[str, list[Any]] = {name: [] for name in ledger.SLICES}
    for record in records:
        for name in ledger.SLICES:
            items = record.get(name, [])
            if isinstance(items, list):
                slices[name].extend(items)
    blockers = verdict.get("blockers", [])
    warnings = verdict.get("warnings", [])
    test_runs = facts.get("test_runs", [])
    # Compatible with verify-ground-truth.py: it reads files/tests.
    # Without them the gate silently skips the check (agy finding S5 R83).
    compat_files = sorted(set(facts.get("touched", []))
                          | set(facts.get("untracked", [])))
    compat_tests = [{"command": r.get("command"), "exit": r.get("exit", 0)}
                    for r in test_runs if isinstance(r, dict)]
    return {
        "version": SUMMARY_VERSION,
        "head": facts.get("head", ""),
        "dirty_hash": facts.get("dirty_hash", ""),
        "touched": list(facts.get("touched", [])),
        "untracked": list(facts.get("untracked", [])),
        "mock_flags": list(facts.get("mock_flags", [])),
        "test_runs": list(test_runs),
        "files": compat_files,
        "tests": compat_tests,
        "ledger": slices,
        "anti_mock": {
            "ok": verdict.get("ok", False),
            "blockers": len(blockers),
            "warnings": len(warnings),
        },
    }


def render(summary: dict[str, Any]) -> str:
    """Human-readable report: numbers and lists from the summary, no prose."""
    lines = [
        "round-summary: head %s dirty %s"
        % (summary.get("head", "")[:12], summary.get("dirty_hash", "")),
        "touched: %d untracked: %d mock-flags: %d test-runs: %d"
        % (len(summary.get("touched", [])),
           len(summary.get("untracked", [])),
           len(summary.get("mock_flags", [])),
           len(summary.get("test_runs", []))),
    ]
    for name in ledger.SLICES:
        items = summary.get("ledger", {}).get(name, [])
        lines.append("%s: %d" % (name, len(items)))
        lines.extend("  - %s" % item for item in items)
    gate = summary.get("anti_mock", {})
    lines.append("anti-mock: %s (%d blockers, %d warnings)"
                 % ("PASS" if gate.get("ok") else "BLOCKED",
                    gate.get("blockers", 0), gate.get("warnings", 0)))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI: round tree facts + ledger -> summary.json on stdout/file."""
    parser = argparse.ArgumentParser(description="Round digest")
    parser.add_argument("--facts", required=True, help="path to the tree facts")
    parser.add_argument("--state-dir", required=True, help="the .pair directory")
    parser.add_argument("--round", required=True, type=int, help="round number")
    parser.add_argument("--out", default="", help="summary.json file")
    args = parser.parse_args(argv)
    try:
        facts = session_facts.load_facts(args.facts)
    except session_facts.FactsError as exc:
        print("round-summary: %s" % exc)
        return 2
    records = ledger.read_round(args.state_dir, args.round)
    verdict = anti_mock_gate.judge(facts)
    summary = build(facts, records, verdict)
    text = json.dumps(summary, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    print(render(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
