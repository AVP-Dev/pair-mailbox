"""Нативные permission-слои CLI: одно правило — три грамматики.

Зачем модуль: боль раунда 54 — человек прокликивает подтверждения рутинных
операций, потому что 5 из 6 профилей cli-profiles.json поднимаются с голым
launch, а install.sh писал deny-блок только для opencode. Разносить правила по
форматам нельзя: три представления одного правила разойдутся при первой же
правке. Поэтому здесь канонический список и рендереры-вью.

Bypass-флаги (`--dangerously-skip-permissions`, `-a never`) намеренно не
используются: они снимают все запреты. Промпт снимается нативным allow-list,
deny остаётся неоспоримым. Где нативного слоя нет (codex, pi) — профиль
остаётся на пилоте, и это решение, а не недоделка.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# Канон: категория -> шаблоны, которые разрешены без человека. NEVER-класс
# (§12: секреты, sudo, сеть, история git, системное удаление) намеренно тут
# отсутствует: он живёт в NEVER (composer.py) и в deny-каждого рендерера.
ALLOW: dict[str, tuple[str, ...]] = {
    # локальный git: §10 требует ветку и коммит
    "git": ("status", "diff", "log", "show", "branch", "remote", "describe",
            "rev-parse", "add", "commit", "checkout", "switch", "stash", "tag",
            "blame", "shortlog", "reflog", "check-ignore"),
    # проверки и сборки. P0 (раунд 75): раннеры тестов (pytest, unittest,
    # bun test, npm test, cargo test, jest...) на хосте НЕ разрешены без
    # человека — только через scripts/run-isolated-test.sh (см. composer
    # NEVER). В каноне остаются линтеры и сборки; deny ниже дублирует запрет.
    "checks": ("ruff", "flake8", "pylint", "mypy",
               "pyright", "eslint", "prettier", "tsc", "black", "isort", "make",
               "bun run", "bun x", "npm run", "vitest", "mocha", "rspec",
               "nox", "tox", "nose"),
    # наши гейты и раннеры (относительные пути, см. composer.py allow)
    "gates": ("bash scripts/", "scripts/", "bin/pairup", "bin/pairpilot",
              "bin/pair-duo", "bin/pairctl", "bin/pairchat", "bin/pairwatch",
              "bin/pairdoctor", "bin/pairboard"),
    # чтение и наблюдение без записи
    "read": ("Read", "read_file", "view", "glob", "grep", "search", "list",
             "list_dir", "cat", "head", "tail", "ls", "find"),
    # отправка работы: фиче-ветка и PR (§10)
    "ship": ("git push", "gh pr", "gh issue", "gh run", "gh workflow"),
}

# §12 + классы, которые запрещены при ЛЮБОМ allow-list. Канон deny: рендереры
# переводят его в свою грамматику; пустой deny у рендерера — дефект, а не режим.
DENY: dict[str, tuple[str, ...]] = {
    "secrets": (".env", "secrets", "tokens", "api_key", "api-key", "passwords",
                "credentials", ".ssh", "id_rsa", "id_ed25519", "/etc/shadow",
                "/etc/passwd", ".pem", ".npmrc", ".aws", ".git-credentials"),
    "system": ("sudo", "rm -rf /", "rm -rf ~", "rm -rf .", "mkfs", "dd if=",
               "killall", "docker", "kubectl", "helm", "terraform"),
    "history": ("git push origin main", "git push origin master",
                "--force", "reset --hard", "clean -f", "filter-branch",
                "reflog expire"),
    "network": ("curl", "wget", "nc -", "ssh", "pip install", "npm install",
                "bun add", "npm publish"),
    # P0 (раунд 75): запуск тестов на хосте — только человек; в нативных слоях
    # CLI это deny, безопасный путь — scripts/run-isolated-test.sh.
    "testruns": ("pytest", "unittest", "cargo test", "go test", "bun test",
                 "npm test", "npm ci", "jest", "node --test"),
}

PROFILES_WITH_NATIVE_LAYER = ("opencode", "agy", "claude")
PROFILES_ON_PILOT_ONLY = ("codex", "pi", "omp")


def _allow_items() -> list[str]:
    out: list[str] = []
    for patterns in ALLOW.values():
        out.extend(patterns)
    return out


def _deny_items() -> list[str]:
    out: list[str] = []
    for patterns in DENY.values():
        out.extend(patterns)
    return out


# Ключ настроек: opencode и claude — `permission`, agy — `permissions`
# (у обоих это словарь с allow/deny/ask, но имя ключа разное, и мерж по
# неверному имени молча оставляет старый блок — так и было до этой константы).
PERM_KEY = {"opencode": "permission", "claude": "permission", "agy": "permissions"}


def autoperms_key(profile: str) -> str:
    return PERM_KEY[profile]


def render_opencode(root: Path) -> dict[str, Any]:
    """opencode.jsonc: bash `*: allow` + deny-глобы, edit/webfetch — ask.

    Формат — glob-строки; порядок в ТУИ не важен, deny всегда сильнее §13c.
    """
    bash: dict[str, str] = {"*": "allow"}
    for item in _deny_items():
        bash[f"*{item}*"] = "deny"
    return {"$schema": "https://opencode.ai/config.json",
            "permission": {"bash": bash, "edit": "ask", "webfetch": "ask"}}


def render_agy(home: Path) -> dict[str, Any]:
    """~/.gemini/antigravity-cli/settings.json: action(target), Deny>Ask>Allow.

    Грамматика из официальной доки Antigravity (agy.dev/docs/permissions):
    command(prefix) как allow, command(sudo)/write_file(.git/) как deny.
    """
    allow, deny = [], ["command(sudo)", "command(rm)", "command(rm -rf)",
                       "write_file(.git/)", "write_file(/home/user/.ssh)"]
    for item in ALLOW["git"]:
        allow.append(f"command(git {item})")
    for item in ALLOW["ship"]:
        allow.append(f"command({item})")
    for item in ALLOW["checks"]:
        allow.append(f"command({item})")
    for item in ALLOW["read"]:
        allow.append(item)
    for item in DENY["secrets"]:
        deny.append(f"write_file({item})")
    for item in ("curl", "wget", "ssh", "nc", "sudo"):
        deny.append(f"command(regex:{item} .*)")
    for item in DENY["testruns"]:
        deny.append(f"command(regex:{item} .*)")
    return {"permissions": {"allow": allow, "deny": deny,
                            "ask": ["command(*)", "read_url(*)"]}}


def render_claude(root: Path) -> dict[str, Any]:
    """Проектный .claude/settings.json: правила Bash(git:*), deny перекрывает.

    Формат Claude Code: `Bash(<cmd>:*)`, `Read(<path>)`. Ask-умолчание
    остаётся умолчанием: allow закрывает только рутину пары.
    """
    allow = [f"Bash(git {item}:*)" for item in ALLOW["git"]]
    allow += [f"Bash({item})" for item in ALLOW["checks"]]
    allow += [f"Bash({item})" for item in ALLOW["gates"]]
    allow += [f"Bash(gh {item}:*)" for item in ("pr", "issue", "run", "workflow")]
    deny = [f"Read(./{item})" for item in DENY["secrets"]]
    deny += ["Bash(curl:*)", "Bash(wget:*)", "Bash(sudo:*)", "Bash(ssh:*)",
             "Bash(pip install:*)", "Bash(npm install:*)", "Bash(bun add:*)",
             "Bash(git push --force:*)", "Bash(git reset --hard:*)"]
    deny += [f"Bash({item}:*)" for item in DENY["testruns"]]
    return {"permission": {"allow": allow, "deny": deny}}


RENDERERS = {
    "opencode": lambda root, home: render_opencode(Path(root)),
    "agy": lambda root, home: render_agy(Path(home)),
    "claude": lambda root, home: render_claude(Path(root)),
}

# Куда каждый рендерер пишет: (пусть, читаемый человеком ярлык)
TARGETS = {
    "opencode": lambda root, home: Path(home) / ".config" / "opencode" / "opencode.jsonc",
    "agy": lambda root, home: Path(home) / ".gemini" / "antigravity-cli" / "settings.json",
    "claude": lambda root, home: Path(root) / ".claude" / "settings.json",
}


def render(profile: str, root: Path, home: Path) -> dict[str, Any]:
    """Собрать настройки профиля. Неизвестный профиль — отказ, не пустышка."""
    if profile not in RENDERERS:
        raise ValueError(f"нет нативного permission-слоя для {profile!r}; "
                         f"известны: {', '.join(sorted(RENDERERS))}")
    payload = RENDERERS[profile](root, home)
    if not _deny_items() and profile != "claude":
        raise ValueError("пустой deny — это дыра, а не режим")
    return payload


def _merge_permission(profile: str, existing: Any, canonical: Any) -> Any:
    """Слияние с чужим блоком: правила человека выживают, канон добавляется.

    Замена блока целиком — это молчаливое уничтожение чужой настройки
    (install_test.py test_unrelated_keys_in_jsonc_survive поймал именно это:
    ручной `sudo *: deny` из фикстуры исчезал после установки). Объединяем:
    у opencode — словарь bash по ключам с приоритетом канона (deny сильнее),
    у agy/claude — объединение списков (в обоих deny перекрывает allow).
    """
    if not isinstance(existing, dict) or not isinstance(canonical, dict):
        return canonical
    merged = dict(existing)
    for key, value in canonical.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            section = dict(merged[key])
            section.update(value)
            merged[key] = section
        elif isinstance(value, list) and isinstance(merged.get(key), list):
            section = list(merged[key])
            section.extend(item for item in value if item not in section)
            merged[key] = section
        else:
            merged[key] = value
    return merged


def write_native_perms(profile: str, root: Path, home: Path) -> Path:
    """Записать настройки и вернуть путь. Слияние с существующим файлом:
    ключ permissions дописывается/заменяется, остальное содержимое цело."""
    target = TARGETS[profile](root, home)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = render(profile, root, home)
    key = autoperms_key(profile)
    merged: dict[str, Any] = {}
    if target.exists():
        try:
            merged = json.loads(target.read_text(encoding="utf-8"))
            if not isinstance(merged, dict):
                merged = {}
        except (OSError, json.JSONDecodeError):
            # Нечитаемый файл не перезаписываем молча: чужая настройка важнее
            # нашей автоматики. Пусть установщик скажет об этом человеку.
            raise
    merged[key] = _merge_permission(profile, merged.get(key), payload.get(key, payload))
    target.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8")
    return target
