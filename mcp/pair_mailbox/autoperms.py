"""Native CLI permission layers: one rule — three grammars.

Why the module stands: the round-54 pain — the human clicks through routine
confirmations because 5 of 6 cli-profiles.json profiles rise with a bare launch,
while install.sh wrote a deny block for opencode only. Spreading rules over formats
is banned: three views of one rule drift on the very first edit. So here the
canonical list plus renderer views.

Bypass flags (`--dangerously-skip-permissions`, `-a never`) stay deliberately
unused: they drop every ban. The prompt lifts by a native allow-list, deny stays
indisputable. Claude's permission-mode auto is paired with a fail-closed native
Bash sandbox; role separation is enforced outside the CLI as well.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# Canon: category -> patterns allowed with no human. The NEVER class (§12:
# secrets, sudo, network, git history, system deletion) deliberately misses here:
# it lives in NEVER (composer.py) and in every-renderer deny.
ALLOW: dict[str, tuple[str, ...]] = {
    # local git: §10 demands a branch and a commit
    "git": ("status", "diff", "log", "show", "branch", "remote", "describe",
            "rev-parse", "add", "commit", "checkout", "switch", "stash", "tag",
            "blame", "shortlog", "reflog", "check-ignore"),
    # checks and builds. P0 (round 75): test runners (pytest, unittest, bun test, npm
    # test, cargo test, jest...) on host stay human-only — only over
    # scripts/run-isolated-test.sh (see composer NEVER). Linters and builds stay in the
    # canon; deny below duplicates the ban.
    "checks": ("ruff", "flake8", "pylint", "mypy",
               "pyright", "eslint", "prettier", "tsc", "black", "isort", "make",
               "bun run", "bun x", "npm run", "vitest", "mocha", "rspec",
               "nox", "tox", "nose"),
    # our gates and runners (relative paths, see composer.py allow)
    "gates": ("bash scripts/", "scripts/", "bin/pairup", "bin/pairpilot",
              "bin/pair-duo", "bin/pairctl", "bin/pairchat", "bin/pairwatch",
              "bin/pairdoctor", "bin/pairboard"),
    # reading and observing with no writes
    "read": ("Read", "read_file", "view", "glob", "grep", "search", "list",
             "list_dir", "cat", "head", "tail", "ls", "find"),
    # shipping work: a feature branch and a PR (§10); push gets a narrower
    # renderer rule so implicit upstream pushes are never auto-authorized.
    "ship": ("gh pr", "gh issue", "gh run", "gh workflow"),
}

# §12 plus classes banned under ANY allow-list. The deny canon: renderers translate
# it into their grammar; an empty renderer deny is a defect, not a mode.
DENY: dict[str, tuple[str, ...]] = {
    "secrets": (".env", "secrets", "tokens", "api_key", "api-key", "passwords",
                "credentials", ".ssh", "id_rsa", "id_ed25519", "/etc/shadow",
                "/etc/passwd", ".pem", ".npmrc", ".aws", ".git-credentials"),
    "system": ("sudo", "rm -rf /", "rm -rf ~", "rm -rf .", "mkfs", "dd if=",
               "killall", "docker", "kubectl", "helm", "terraform"),
    "history": ("git merge", "git push origin main", "git push origin master",
                "--force", "-f", "reset --hard", "clean -f", "filter-branch",
                "reflog expire"),
    "network": ("curl", "wget", "nc -", "ssh", "pip install", "npm install",
                "bun add", "npm publish"),
    # P0 (round 75): host test runs mean human only; in native CLI layers that is deny,
    # the safe path scripts/run-isolated-test.sh.
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


# Settings key: opencode and claude take `permission`, agy `permissions` (both a
# dict with allow/deny/ask, but the key name differs, and merging under the wrong
# name silently keeps the old block — as it was before this constant).
PERM_KEY = {"opencode": "permission", "claude": "permission", "agy": "permissions"}


def autoperms_key(profile: str) -> str:
    return PERM_KEY[profile]


def render_opencode(root: Path) -> dict[str, Any]:
    """opencode.jsonc: allow normal work, ask on pushes, and deny unsafe commands.

    OpenCode uses last-matching-rule semantics. Keep the catch-all first, then the
    narrowly scoped push rules, then protected branch and history denials.
    """
    bash: dict[str, str] = {"*": "allow"}
    bash["*git push*"] = "ask"
    bash["*git -C * push*"] = "ask"
    bash["*git push [!-]* [!-]*"] = "allow"
    bash["*git push -u [!-]* [!-]*"] = "allow"
    bash["*git push --set-upstream [!-]* [!-]*"] = "allow"
    bash["*git -C * push [!-]* [!-]*"] = "allow"
    bash["*git -C * push -u [!-]* [!-]*"] = "allow"
    bash["*git -C * push --set-upstream [!-]* [!-]*"] = "allow"
    for item in _deny_items():
        bash[f"*{item}*"] = "deny"
    bash["*git merge*"] = "deny"
    bash["*git -C * merge*"] = "deny"
    for branch in ("main", "master", "dev"):
        bash[f"*git push *{branch}*"] = "deny"
        bash[f"*git -C * push *{branch}*"] = "deny"
    bash["*git push *--force*"] = "deny"
    bash["*git push -f*"] = "deny"
    bash["*git -C * push *--force*"] = "deny"
    bash["*git -C * push -f*"] = "deny"
    return {"$schema": "https://opencode.ai/config.json",
            "permission": {"bash": bash, "edit": "ask", "webfetch": "ask"}}


def render_agy(home: Path) -> dict[str, Any]:
    """~/.gemini/antigravity-cli/settings.json: action(target), Deny>Ask>Allow.

    Grammar from the official Antigravity docs (agy.dev/docs/permissions):
    command(prefix) as allow, command(sudo)/write_file(.git/) as deny.
    """
    allow, deny = [], ["command(sudo)", "command(rm)", "command(rm -rf)",
                       "write_file(.git/)", "write_file(/home/user/.ssh)"]
    for item in ALLOW["git"]:
        allow.append(f"command(git {item})")
    for item in ALLOW["ship"]:
        allow.append(f"command({item})")
    allow.append(
        r"command(regex:git (?:-C \S+ )?push (?:-u |--set-upstream )?[^-\s]\S* [^-\s]\S*.*)"
    )
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
    for item in DENY["history"]:
        deny.append(f"command(regex:.*{item}.*)")
    for branch in ("main", "master", "dev"):
        deny.append(f"command(regex:git (?:-C \\S+ )?push .*\\b{branch}\\b)")
    deny.append(r"command(regex:git (?:-C \S+ )?push(?:\s+\S+)?$)")
    deny.append(r"command(regex:git (?:-C \S+ )?merge(?:\s|$).*)")
    return {"permissions": {"allow": allow, "deny": deny,
                            "ask": ["read_url(*)"]}}


def render_claude(root: Path) -> dict[str, Any]:
    """Project .claude/settings.json: Bash(git:*) rules, deny overrides.

    The Claude Code format: `Bash(<cmd>:*)`, `Read(<path>)`. The ask default stays
    the default: allow closes only pair routine.
    """
    allow = [f"Bash(git {item}:*)" for item in ALLOW["git"]]
    allow += [f"Bash({item})" for item in ALLOW["checks"]]
    allow += [f"Bash({item})" for item in ALLOW["gates"]]
    allow += [f"Bash(gh {item}:*)" for item in ("pr", "issue", "run", "workflow")]
    deny = [f"Read(./{item})" for item in DENY["secrets"]]
    deny += ["Bash(curl:*)", "Bash(wget:*)", "Bash(sudo:*)", "Bash(ssh:*)",
             "Bash(pip install:*)", "Bash(npm install:*)", "Bash(bun add:*)",
             "Bash(git merge:*)", "Bash(git push --force:*)", "Bash(git reset --hard:*)"]
    deny += [f"Bash({item}:*)" for item in DENY["testruns"]]
    return {
        "permission": {"allow": allow, "deny": deny},
        "sandbox": {
            "enabled": True,
            "failIfUnavailable": True,
            "allowUnsandboxedCommands": False,
            "enableWeakerNestedSandbox": False,
            "enableWeakerNetworkIsolation": False,
        },
    }


RENDERERS = {
    "opencode": lambda root, home: render_opencode(Path(root)),
    "agy": lambda root, home: render_agy(Path(home)),
    "claude": lambda root, home: render_claude(Path(root)),
}

# Where each renderer writes: (path, human-readable label)
TARGETS = {
    "opencode": lambda root, home: Path(home) / ".config" / "opencode" / "opencode.jsonc",
    "agy": lambda root, home: Path(home) / ".gemini" / "antigravity-cli" / "settings.json",
    "claude": lambda root, home: Path(root) / ".claude" / "settings.json",
}


def render(profile: str, root: Path, home: Path) -> dict[str, Any]:
    "Assemble the profile settings. An unknown profile refuses, never a dummy."
    if profile not in RENDERERS:
        raise ValueError(f"no native permission layer for {profile!r}; "
                         f"known: {', '.join(sorted(RENDERERS))}")
    payload = RENDERERS[profile](root, home)
    if not _deny_items() and profile != "claude":
        raise ValueError("an empty deny is a hole, not a mode")
    return payload


def _merge_permission(profile: str, existing: Any, canonical: Any) -> Any:
    """Merging with a foreign block: human rules survive, the canon adds.

    Replacing the block whole is a silent kill of a foreign setting (install_test.py
    test_unrelated_keys_in_jsonc_survive caught exactly that: a hand `sudo *:
    deny` from the fixture vanished past install). Merging: opencode takes a bash
    dict by keys with canon priority (deny beats), agy/claude take list union
    (deny overrides allow in both).
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
    """Write the settings returning the path. Merging with an existing file: the
    permissions key appends/replaces, the rest stays whole."""
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
            # An unreadable file never rewrites silent: a foreign setting outweighs our
            # automation. Let the installer tell the human about it.
            raise
    merged[key] = _merge_permission(profile, merged.get(key), payload.get(key, payload))
    target.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8")
    return target
