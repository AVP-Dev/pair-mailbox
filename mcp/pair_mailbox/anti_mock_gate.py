"""Anti-mock gate: вердикт по mock-флагам facts.json.

Зачем: сбор фактов находит подозрительные строки, но решение «блокировать
или пропустить» — отдельная ответственность. Тестовые пути — предупреждение,
а не блок: фикстуры в тестах законны. Боевые пути — блок: мок вместо wiring
(урок R81) не проходит молча. Рантайм — stdlib, входа только facts.json.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import session_facts


def judge(facts: dict[str, Any]) -> dict[str, Any]:
    """Разделить mock-флаги: боевые — блок, тестовые — предупреждение."""
    blockers: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    for flag in facts.get("mock_flags", []):
        (blockers if not flag.get("test_path") else warnings).append(flag)
    local_state = [f for f in blockers if f.get("pattern") == "local-state"]
    for flag in local_state:
        flag["reason"] += " — решает критик (S5): транспорт или изоляция?"
    return {
        "ok": not blockers,
        "blockers": blockers,
        "warnings": warnings,
        "head": facts.get("head", ""),
        "dirty_hash": facts.get("dirty_hash", ""),
    }


def render(verdict: dict[str, Any]) -> str:
    """Человекочитаемый отчёт: file:line + причина по каждому флагу."""
    lines = [f"anti-mock: {'PASS' if verdict['ok'] else 'BLOCKED'} "
             f"({len(verdict['blockers'])} blockers, "
             f"{len(verdict['warnings'])} warnings)"]
    for flag in verdict["blockers"]:
        lines.append(f"BLOCK {flag['file']}:{flag['line']} "
                     f"[{flag['pattern']}] {flag['reason']} "
                     f"(совпало: {flag['match']})")
    for flag in verdict["warnings"]:
        lines.append(f"warn {flag['file']}:{flag['line']} "
                     f"[{flag['pattern']}] тестовый путь")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI: проверить facts.json, exit 1 при боевых моках."""
    parser = argparse.ArgumentParser(description="Проверить facts.json на моки")
    parser.add_argument("facts", help="путь к facts.json")
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
