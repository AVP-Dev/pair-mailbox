"""Anti-mock gate: the verdict over the tree facts' mock flags.

Why: fact collecting finds suspicious lines, but the "block or pass" decision is a
separate responsibility. Test paths warn, never block: fixtures in tests are lawful.
Live paths block: a mock instead of wiring (the R81 lesson) never passes silent.
Runtime is stdlib, the only input is the tree facts file.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import session_facts


def judge(facts: dict[str, Any]) -> dict[str, Any]:
    "Splitting mock flags: live ones block, test ones warn."
    blockers: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    for flag in facts.get("mock_flags", []):
        (blockers if not flag.get("test_path") else warnings).append(flag)
    local_state = [f for f in blockers if f.get("pattern") == "local-state"]
    for flag in local_state:
        flag["reason"] += " - the critic decides (S5): transport or isolation?"
    return {
        "ok": not blockers,
        "blockers": blockers,
        "warnings": warnings,
        "head": facts.get("head", ""),
        "dirty_hash": facts.get("dirty_hash", ""),
    }


def render(verdict: dict[str, Any]) -> str:
    "A human-readable report: file:line plus a reason per flag."
    lines = [f"anti-mock: {'PASS' if verdict['ok'] else 'BLOCKED'} "
             f"({len(verdict['blockers'])} blockers, "
             f"{len(verdict['warnings'])} warnings)"]
    for flag in verdict["blockers"]:
        lines.append(f"BLOCK {flag['file']}:{flag['line']} "
                     f"[{flag['pattern']}] {flag['reason']} "
                     f"(matched: {flag['match']})")
    for flag in verdict["warnings"]:
        lines.append(f"warn {flag['file']}:{flag['line']} "
                     f"[{flag['pattern']}] test path")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    "CLI: check the tree facts, exit 1 on live mocks."
    parser = argparse.ArgumentParser(
        description="Check the tree facts for mocks")
    parser.add_argument("facts", help="the tree facts path")
    args = parser.parse_args(argv)
    try:
        facts = session_facts.load_facts(args.facts)
    except session_facts.FactsError as exc:
        print(f"anti-mock: {exc}")
        return 2
    verdict = judge(facts)
    print(render(verdict))
    return 0 if verdict["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
