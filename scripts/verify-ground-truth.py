#!/usr/bin/env python3
"""Ground-truth gate: checking summary claims against facts.json.

Why: the worker summary claims "files changed, tests run" - the gate checks every
claim against deterministic facts, blocking (exit 1) on mismatch. Fail-closed: a
broken facts.json, a foreign head or a mismatched dirty-hash blocks too, never
passes.
Summary input is JSON: {"files": [...], "tests": [{"command": ..., "exit": 0}]}.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mcp" / "pair_mailbox"))
import session_facts


def verify(summary: dict, facts: dict) -> list[str]:
    "Check the summary against facts, returning the mismatch list."
    problems: list[str] = []
    if summary.get("head") and summary.get("head") != facts.get("head"):
        problems.append(
            f"the summary head {summary['head'][:12]} != facts {facts.get('head', '')[:12]}")
    if summary.get("dirty_hash") and summary.get("dirty_hash") != facts.get("dirty_hash"):
        problems.append("the summary dirty-hash mismatched facts.json - the tree changed")
    known = set(facts.get("touched", [])) | set(facts.get("untracked", []))
    files = (summary.get("files") if "files" in summary
             else list(summary.get("touched", []))
             + list(summary.get("untracked", [])))
    for path in files:
        if path not in known:
            problems.append(f"file {path} claimed, but git never shows it")
    runs = {(r.get("command"), r.get("exit")) for r in facts.get("test_runs", [])}
    tests = (summary.get("tests") if "tests" in summary
             else summary.get("test_runs", []))
    for test in tests:
        if (test.get("command"), test.get("exit")) not in runs:
            problems.append(
                f"test {test.get('command')} exit {test.get('exit')} "
                f" never recorded in facts.json")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check the summary against facts.json")
    parser.add_argument("summary", help="The worker summary JSON")
    parser.add_argument("facts", help="the facts.json path")
    args = parser.parse_args(argv)
    try:
        summary = json.loads(Path(args.summary).read_text(encoding="utf-8"))
        facts = session_facts.load_facts(args.facts)
    except (OSError, json.JSONDecodeError, session_facts.FactsError) as exc:
        print(f"ground-truth: BLOCKED - input never reads: {exc}")
        return 2
    problems = verify(summary, facts)
    if problems:
        print("ground-truth: BLOCKED")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("ground-truth: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
