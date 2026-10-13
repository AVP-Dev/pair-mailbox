"""Ground-truth facts: детерминированный снимок состояния репозитория.

Зачем: заявления агента сверяются с физическим состоянием дерева, а не с
памятью агента. Прецедент — инцидент R81: зелёные unit-тесты засчитали за
подключённый UI, хотя в браузере осталась статическая коллекция, а кнопки
слали хардкодные id. Сбор только читает (git status/diff + чтение файлов),
рантайм — stdlib + git CLI.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

FACTS_VERSION = 1
FACTS_FILE = "facts.json"


class FactsError(Exception):
    """Факты собрать нельзя: отказ с причиной, а не пустой отчёт."""


def _git(repo: Path, *args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise FactsError(f"git не запустился: {exc}")
    if proc.returncode != 0:
        detail = proc.stderr.strip().splitlines()
        raise FactsError(
            f"git {' '.join(args)}: exit {proc.returncode}: "
            f"{detail[0][:200] if detail else '—'}")
    return proc.stdout


def _unquote(path: str) -> str:
    path = path.strip()
    if len(path) >= 2 and path[0] == '"' and path[-1] == '"':
        return path[1:-1]
    return path

# mock-usage ловит только кодовое использование (вызов/импорт/атрибут), а не
# упоминание слова в комментарии: иначе детектор флагает собственные исходники
# и имена сюит вроде anti-mock (живой пруф R81). (?<![-\w]) вместо \b: дефис
# не граница слова, и без lookbehind anti-mock давал mock ( + совпадение.
MOCK_PATTERNS: tuple[tuple[str, "re.Pattern[str]", str], ...] = (
    ("mock-usage",
     re.compile(r"(?<![-\w])(mock\s*\.\s*\w+|Mock\s*\(|mock\s*\(|stub\s*\(|"
                r"Stub\s*\(|fake\s*\(|Fake\s*\(|(from|import)\s+mock\b)"),
     "использование мок-объекта в боевом пути"),
    ("initial-static",
     re.compile(r"\bINITIAL_[A-Z0-9_]+\b"),
     "статическая INITIAL_-коллекция в изменённом пути"),
    ("mock-id",
     re.compile(r"['\"]mock-[A-Za-z0-9_-]+['\"]"),
     "захардкоженный mock-id вместо выбранного значения"),
    ("includes-len",
     re.compile(r"includes\(\s*['\"]"),
     "подстрочная проверка includes('...') вместо реального предиката"),
    ("local-state",
     re.compile(r"\buseState\s*\("),
     "локальный useState без видимого транспорта — решает критик"),
)
CONFIG_NAME = "anti-mock.json"


def _find_config(start: Path | str) -> Path | None:
    """Искать anti-mock.json вверх от start до корня репозитория."""
    current = Path(start).resolve()
    for _ in range(32):
        candidate = current / CONFIG_NAME
        if candidate.is_file():
            return candidate
        if (current / ".git").exists() or current.parent == current:
            return None
        current = current.parent
    return None


def load_config(repo_root=None):
    """Весь anti-mock.json; без/битый — пустой dict (все детекторы вкл)."""
    found = _find_config(repo_root) if repo_root is not None else None
    if found is None:
        return {}
    try:
        data = json.loads(found.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def stubs_enabled(repo_root=None, config=None) -> bool:
    """Детектор заглушек вкл, пока конфиг явно не выключил (detect_stubs)."""
    cfg = config if config is not None else load_config(repo_root)
    return cfg.get("detect_stubs", True) is not False


def load_patterns(
    repo_root: Path | str | None = None,
) -> tuple[tuple[str, "re.Pattern[str]", str], ...]:
    """Паттерны из версионируемого anti-mock.json; без/битый — дефолт."""
    found = _find_config(repo_root) if repo_root is not None else None
    if found is None:
        return MOCK_PATTERNS
    try:
        data = json.loads(found.read_text(encoding="utf-8"))
        items = data.get("patterns", [])
        loaded = tuple(
            (str(item["name"]), re.compile(str(item["regex"])),
             str(item.get("reason", "")))
            for item in items
            if isinstance(item, dict) and item.get("name")
            and item.get("regex"))
    except (OSError, ValueError, re.error):
        return MOCK_PATTERNS
    return loaded or MOCK_PATTERNS


TEST_HINTS = ("test_", "_test", "tests/", "__tests__",
              ".test.", ".spec.", "spec/", "conftest")

# Пути самого сканера и конфиги проверок: они описывают проверку, а не
# мок-логику приложения (находка agy seq 1065: гейт блокировал сам себя).
GATE_EXEMPT_SUFFIXES = (
    "mcp/pair_mailbox/session_facts.py",
    "mcp/pair_mailbox/anti_mock_gate.py",
    "anti-mock.json",
)
GATE_EXEMPT_EXTENSIONS = (".tsv",)


def is_gate_exempt(path: str) -> bool:
    """Пути самой проверки: exempt из вердикта, не предупреждение."""
    norm = path.replace("\\", "/")
    return norm.endswith(GATE_EXEMPT_SUFFIXES) or norm.endswith(GATE_EXEMPT_EXTENSIONS)


def is_test_path(path: str) -> bool:
    """Эвристика боевого пути: совпадение с тестовым хинтом — не боевой."""
    low = path.replace("\\", "/").lower()
    return any(hint in low for hint in TEST_HINTS)


def parse_status(porcelain: str) -> tuple[list[str], list[str]]:
    """Разобрать status --porcelain: (изменённые, неотслеживаемые)."""
    touched: list[str] = []
    untracked: list[str] = []
    for line in porcelain.splitlines():
        if len(line) < 4:
            continue
        codes, raw = line[:2], _unquote(line[3:])
        if "->" in raw:
            raw = _unquote(raw.split("->")[-1].strip())
        if codes == "??":
            untracked.append(raw)
        elif codes.strip():
            touched.append(raw)
    return touched, untracked


def _parse_added(diff_text: str) -> list[tuple[str, int, str]]:
    """Добавленные строки diff -U0 с номерами строк нового файла."""
    found: list[tuple[str, int, str]] = []
    current: str | None = None
    new_line = 0
    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            target = line[4:].strip()
            if target == "/dev/null":
                current = None
            else:
                current = target[2:] if target.startswith("b/") else target
        elif line.startswith("@@ "):
            match = re.search(r"\+(\d+)", line)
            new_line = int(match.group(1)) if match else 0
        elif current is not None:
            if line.startswith("+") and not line.startswith("+++"):
                found.append((current, new_line, line[1:]))
                new_line += 1
            elif line.startswith("-") and not line.startswith("---"):
                pass
            elif not line.startswith("\\"):
                new_line += 1
    return found


ABSTRACT_DECORATORS = ("abstractmethod",)


def _is_stub_body(node) -> bool:
    """Тело-заглушка: только pass / ... / raise NotImplementedError."""
    body = [st for st in node.body
            if not (isinstance(st, ast.Expr)
                    and isinstance(st.value, ast.Constant)
                    and isinstance(st.value.value, str))]
    if not body:
        return False
    for st in body:
        if isinstance(st, ast.Pass):
            continue
        if (isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant)
                and st.value.value is Ellipsis):
            continue
        if (isinstance(st, ast.Raise)
                and isinstance(st.exc, ast.Name)
                and st.exc.id == "NotImplementedError"):
            continue
        if (isinstance(st, ast.Raise)
                and isinstance(st.exc, ast.Call)
                and isinstance(st.exc.func, ast.Name)
                and st.exc.func.id == "NotImplementedError"):
            continue
        return False
    return True


def _is_abstract(node) -> bool:
    """Абстрактный метод — контракт, а не заглушка."""
    for dec in node.decorator_list:
        name = dec.id if isinstance(dec, ast.Name) else (
            dec.attr if isinstance(dec, ast.Attribute) else "")
        if name in ABSTRACT_DECORATORS:
            return True
    return False


def scan_stubs(path, source) -> list:
    """AST-поиск функций-заглушек; не-Python и битый синтаксис — пусто."""
    if is_gate_exempt(path) or not path.endswith(".py"):
        return []
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []
    flags = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if _is_abstract(node) or not _is_stub_body(node):
            continue
        flags.append({
            "file": path,
            "line": node.lineno,
            "pattern": "stub-body",
            "match": "def %s(...): ..." % node.name,
            "reason": "пустое тело функции — заглушка вместо реализации",
            "test_path": is_test_path(path),
        })
    return flags


def scan_text(path, start, lines, patterns=None):
    """Scan lines; default patterns are MOCK_PATTERNS."""
    if is_gate_exempt(path):
        return []
    active = patterns if patterns is not None else MOCK_PATTERNS
    flags: list[dict[str, Any]] = []
    for offset, text in enumerate(lines):
        for name, pattern, reason in active:
            match = pattern.search(text)
            if match:
                flags.append({
                    "file": path,
                    "line": start + offset,
                    "pattern": name,
                    "match": match.group(0)[:80],
                    "reason": reason,
                    "test_path": is_test_path(path),
                })
    return flags


def collect_facts(repo_root, test_runs=None, patterns=None):
    """Collect facts.json. Patterns resolve from anti-mock.json
    unless passed explicitly."""
    repo = Path(repo_root)
    active = patterns if patterns is not None else load_patterns(repo)
    head = _git(repo, "rev-parse", "HEAD").strip()
    # -uall: неотслеживаемые каталоги раскрывать пофайлово, иначе untracked
    # "tests/" читается как директория и молча пропускается (урок R81).
    porcelain = _git(repo, "status", "--porcelain", "-uall")
    touched, untracked = parse_status(porcelain)
    dirty_hash = hashlib.sha256(porcelain.encode()).hexdigest()[:16]
    stat = _git(repo, "diff", "HEAD", "--stat", "--", ".") if touched else ""
    flags: list[dict[str, Any]] = []
    want_stubs = stubs_enabled(repo)
    if touched:
        added = _parse_added(_git(repo, "diff", "HEAD", "-U0", "--", "."))
        for path, line, text in added:
            flags.extend(scan_text(path, line, [text], active))
        if want_stubs:
            for path in {p for p, _, _ in added if p.endswith(".py")}:
                try:
                    src = (repo / path).read_text(encoding="utf-8",
                                                  errors="strict")
                except (OSError, UnicodeDecodeError):
                    continue
                flags.extend(scan_stubs(path, src))
    for path in untracked:
        try:
            content = (repo / path).read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeDecodeError):
            continue
        flags.extend(scan_text(path, 1, content.splitlines(), active))
        if want_stubs:
            flags.extend(scan_stubs(path, content))
    return {
        "version": FACTS_VERSION,
        "repo": str(repo),
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "head": head,
        "dirty_hash": dirty_hash,
        "touched": sorted(set(touched)),
        "untracked": sorted(set(untracked)),
        "diff_stat": stat,
        "mock_flags": flags,
        "test_runs": list(test_runs or []),
    }


def save_facts(state_dir: Path | str, facts: dict[str, Any]) -> Path:
    """Записать facts.json в каталог состояния, вернуть путь."""
    target = Path(state_dir) / FACTS_FILE
    target.write_text(json.dumps(facts, indent=2, ensure_ascii=False)
                      + "\n", encoding="utf-8")
    return target


def load_facts(path: Path | str) -> dict[str, Any]:
    """Прочитать facts.json; битый файл — FactsError, а не пустота."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FactsError(f"facts.json не читается: {exc}")
    if not isinstance(data, dict) or data.get("version") != FACTS_VERSION:
        raise FactsError("facts.json чужой версии или не словарь")
    return data


def record_test_run(facts_path: Path | str, command: str,
                    exit_code: int, output: str = "") -> dict[str, Any]:
    """Добавить прогон в facts.json: команда, exit, хвост вывода."""
    facts = load_facts(facts_path)
    facts.setdefault("test_runs", []).append({
        "command": command,
        "exit": exit_code,
        "output_tail": output[-2000:],
        "at": datetime.now(timezone.utc).isoformat(),
    })
    Path(facts_path).write_text(
        json.dumps(facts, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return facts


def main(argv: list[str] | None = None) -> int:
    """CLI: собрать факты дерева и записать facts.json."""
    parser = argparse.ArgumentParser(description="Собрать facts.json дерева")
    parser.add_argument("--root", default=".", help="корень репозитория")
    parser.add_argument("--out", default=".pair/facts.json",
                        help="куда записать facts.json")
    args = parser.parse_args(argv)
    try:
        facts = collect_facts(args.root)
    except FactsError as exc:
        print(f"session_facts: {exc}")
        return 2
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(facts, indent=2, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    print(f"session_facts: {len(facts['touched'])} touched, "
          f"{len(facts['mock_flags'])} mock-flags -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
