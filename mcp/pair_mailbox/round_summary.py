"""Round summary: машиночитаемая выжимка раунда для verify-ground-truth.

Зачем: саммари исполнителя сверяется с физическими фактами, а не с
памятью исполнителя (урок R81). Экстрактор только компонует готовые
структуры — facts.json, записи ledger.jsonl, вердикт anti-mock — и не
выдумывает ничего сверх них. Библиотека (build/render) + тонкий CLI.
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
    """Собрать summary: только агрегация входов, без новых утверждений."""
    slices: dict[str, list[Any]] = {name: [] for name in ledger.SLICES}
    for record in records:
        for name in ledger.SLICES:
            items = record.get(name, [])
            if isinstance(items, list):
                slices[name].extend(items)
    blockers = verdict.get("blockers", [])
    warnings = verdict.get("warnings", [])
    return {
        "version": SUMMARY_VERSION,
        "head": facts.get("head", ""),
        "dirty_hash": facts.get("dirty_hash", ""),
        "touched": list(facts.get("touched", [])),
        "untracked": list(facts.get("untracked", [])),
        "mock_flags": list(facts.get("mock_flags", [])),
        "test_runs": list(facts.get("test_runs", [])),
        "ledger": slices,
        "anti_mock": {
            "ok": verdict.get("ok", False),
            "blockers": len(blockers),
            "warnings": len(warnings),
        },
    }


def render(summary: dict[str, Any]) -> str:
    """Человекочитаемый отчёт: числа и списки из summary, без прозы."""
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
    """CLI: facts.json + ledger раунда -> summary.json на stdout/файл."""
    parser = argparse.ArgumentParser(description="Выжимка раунда")
    parser.add_argument("--facts", required=True, help="путь к facts.json")
    parser.add_argument("--state-dir", required=True, help="каталог .pair")
    parser.add_argument("--round", required=True, type=int, help="номер раунда")
    parser.add_argument("--out", default="", help="файл summary.json")
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
