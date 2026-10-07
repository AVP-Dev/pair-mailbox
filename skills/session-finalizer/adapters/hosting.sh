#!/usr/bin/env bash
# hosting.sh - base-branch and PR evidence via configured providers, else
# UNKNOWN. Provider order comes from config hosting.providers (default
# gh, glab); an absent or failing provider is never success: callers
# treat UNKNOWN as "no proof" and stay conservative, never as clean.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
SKILL_DIR="$(cd "$HERE/.." && pwd)"

# Providers in config order (hosting.providers), filtered to installed.
hosting_providers() {
  python3 - "$SKILL_DIR/config/defaults.json" <<'PY' 2>/dev/null
import json, shutil, sys
try:
    cfg = json.load(open(sys.argv[1]))
    provs = cfg.get("hosting", {}).get("providers", ["gh", "glab"])
except Exception:
    provs = ["gh", "glab"]
print("\n".join([p for p in provs if shutil.which(p)]))
PY
}

# First configured-and-installed provider, else UNKNOWN.
hosting_detect() {
  local p
  p=$(hosting_providers | head -1)
  if [ -n "${p:-}" ]; then echo "$p"; else echo "UNKNOWN"; fi
}

# Default branch from the hosting API, or UNKNOWN (bounded: the CLI
# may hit the network, so guard with timeout).
hosting_base() {
  local p b
  p=$(hosting_detect)
  if [ "$p" = "gh" ]; then
    b=$(timeout 15 gh repo view --json defaultBranchRef --jq '.defaultBranchRef.name' 2>/dev/null) || true
    [ -n "${b:-}" ] && { echo "$b"; return 0; }
  elif [ "$p" = "glab" ]; then
    b=$(timeout 15 glab repo view --output json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin).get("default_branch",""))' 2>/dev/null) || true
    [ -n "${b:-}" ] && { echo "$b"; return 0; }
  fi
  echo "UNKNOWN"
}

# PR for $branch as JSON, or the literal string UNKNOWN. Normalised by
# finalize-check into {provider,number,state,url} or UNKNOWN, never
# mixed shapes, so assertions stay total.
hosting_pr() {
  local branch="${1:-$(git branch --show-current 2>/dev/null)}" p out
  p=$(hosting_detect)
  if [ "$p" = "gh" ]; then
    out=$(timeout 15 gh pr view "$branch" --json number,state,url 2>/dev/null) || true
    if [ -n "${out:-}" ] && python3 -c 'import json,sys; json.loads(sys.argv[1])' "$out" 2>/dev/null; then
      python3 - "$out" <<'PY' 2>/dev/null || echo "UNKNOWN"
import json, sys
d = json.loads(sys.argv[1])
print(json.dumps({"provider": "gh", "number": d.get("number"),
                  "state": d.get("state"), "url": d.get("url")}))
PY
      return 0
    fi
  elif [ "$p" = "glab" ]; then
    out=$(timeout 15 glab mr view --output json 2>/dev/null) || true
    if [ -n "${out:-}" ]; then
      python3 - "$out" <<'PY' 2>/dev/null || echo "UNKNOWN"
import json, sys
try:
    d = json.loads(sys.argv[1])
except Exception:
    print("UNKNOWN")
    raise SystemExit(0)
iid = d.get("iid", d.get("number"))
print(json.dumps({"provider": "glab", "number": iid,
                  "state": d.get("state"), "url": d.get("web_url", d.get("url"))}))
PY
      return 0
    fi
  fi
  echo "UNKNOWN"
}

hosting_help() {
  echo "Usage: hosting.sh {detect|base|pr [branch]}"
  echo "  detect - first configured provider (hosting.providers) or UNKNOWN"
  echo "  base   - default branch via API or UNKNOWN"
  echo "  pr     - PR JSON {provider,number,state,url} or UNKNOWN"
}

case "${1:-}" in
  --help|-h) hosting_help; exit 0;;
  detect) hosting_detect;;
  base) hosting_base;;
  pr) hosting_pr "${2:-}";;
  "") hosting_help; exit 1;;
  *) echo "unknown subcommand: $1" >&2; hosting_help; exit 1;;
esac
