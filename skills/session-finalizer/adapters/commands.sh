#!/usr/bin/env bash
# commands.sh - detect project test/lint/build/typecheck commands.
# Precedence is package.json scripts, then Makefile targets, then
# python files, go.mod, Taskfile; each slot is a command or UNKNOWN.
# UNKNOWN means "no evidence", never a failure signal by itself.
set -u

# Print JSON {test,lint,build,typecheck} for $repo (default cwd).
commands_detect() {
  local repo="${1:-$(pwd)}"
  python3 - "$repo" <<'PY'
import json, os, re, sys
repo = sys.argv[1]
def has(p):
    return os.path.exists(os.path.join(repo, p))
def pkg_scripts():
    try:
        return json.load(open(os.path.join(repo, "package.json"))).get("scripts", {})
    except Exception:
        return {}
def mk_targets():
    try:
        t = []
        for line in open(os.path.join(repo, "Makefile")):
            m = re.match(r'^([A-Za-z0-9_.-]+)\s*:[^=]', line)
            if m:
                t.append(m.group(1))
        return t
    except Exception:
        return []
out = {"test": "UNKNOWN", "lint": "UNKNOWN", "build": "UNKNOWN", "typecheck": "UNKNOWN"}
if has("package.json"):
    s = pkg_scripts()
    out["test"] = "npm test" if "test" in s else "UNKNOWN"
    out["lint"] = "npm run lint" if "lint" in s else "UNKNOWN"
    out["build"] = "npm run build" if "build" in s else "UNKNOWN"
    out["typecheck"] = "npm run typecheck" if "typecheck" in s else "UNKNOWN"
elif has("Makefile"):
    m = mk_targets()
    out["test"] = "make test" if "test" in m else "UNKNOWN"
    out["lint"] = "make lint" if "lint" in m else "UNKNOWN"
    out["build"] = "make build" if "build" in m else "UNKNOWN"
    out["typecheck"] = "make typecheck" if "typecheck" in m else "UNKNOWN"
elif has("pyproject.toml") or has("requirements.txt") or has("setup.py"):
    out["test"] = "pytest"
    out["lint"] = "ruff check ." if has("pyproject.toml") else "UNKNOWN"
    out["typecheck"] = "mypy ." if has("pyproject.toml") else "UNKNOWN"
elif has("go.mod"):
    out["test"] = "go test ./..."
    out["build"] = "go build ./..."
    out["typecheck"] = "go vet ./..."
elif has("Taskfile.yml") or has("Taskfile.yaml"):
    out["test"] = "task test"
    out["lint"] = "task lint"
    out["build"] = "task build"
print(json.dumps(out))
PY
}

commands_help() {
  echo "Usage: commands.sh {detect [repo]}"
  echo "  detect - JSON {test,lint,build,typecheck} with UNKNOWN when absent"
}

case "${1:-}" in
  --help|-h) commands_help; exit 0;;
  detect) commands_detect "${2:-$(pwd)}";;
  "") commands_help; exit 1;;
  *) echo "unknown subcommand: $1" >&2; commands_help; exit 1;;
esac
