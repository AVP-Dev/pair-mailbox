#!/usr/bin/env python3
"""Ground-truth gate: сверка заявлений саммари против facts.json.

Зачем: саммари исполнителя утверждает «файлы изменены, тесты прогнаны» —
гейт сверяет каждое утверждение с детерминированными фактами и блокирует
(exit 1) при расхождении. Fail-closed: битый facts.json, чужой head или
несовпавший dirty-hash — тоже блок, а не пропуск.
Вход саммари — JSON: {"files": [...], "tests": [{"command": ..., "exit": 0}]}.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mcp" / "pair_mailbox"))
import session_facts


def verify(summary: dict, facts: dict) -> list[str]:
    """Сверить саммари с фактами, вернуть список расхождений."""
    problems: list[str] = []
    if summary.get("head") and summary.get("head") != facts.get("head"):
        problems.append(
            f"head саммари {summary['head'][:12]} != facts {facts.get('head', '')[:12]}")
    if summary.get("dirty_hash") and summary.get("dirty_hash") != facts.get("dirty_hash"):
        problems.append("dirty-hash саммари не совпал с facts.json — дерево изменилось")
    known = set(facts.get("touched", [])) | set(facts.get("untracked", []))
    for path in summary.get("files", []):
        if path not in known:
            problems.append(f"файл {path} заявлен, но git его не показывает")
    runs = {(r.get("command"), r.get("exit")) for r in facts.get("test_runs", [])}
    for test in summary.get("tests", []):
        if (test.get("command"), test.get("exit")) not in runs:
            problems.append(
                f"тест {test.get('command')} exit {test.get('exit')} "
                f"не записан в facts.json")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Сверить саммари с facts.json")
    parser.add_argument("summary", help="JSON саммари исполнителя")
    parser.add_argument("facts", help="путь к facts.json")
    args = parser.parse_args(argv)
    try:
        summary = json.loads(Path(args.summary).read_text(encoding="utf-8"))
        facts = session_facts.load_facts(args.facts)
    except (OSError, json.JSONDecodeError, session_facts.FactsError) as exc:
        print(f"ground-truth: BLOCKED — вход не читается: {exc}")
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
