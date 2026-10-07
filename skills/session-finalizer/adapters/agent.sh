#!/usr/bin/env bash
# agent.sh - agent registry, session artifacts, close order.
# Registry = cli-profiles.json top-level keys (minus _-private) plus
# agents_config_overrides from the skill config plus session_config
# (.session/finalizer.json {agents:{name:{globs}}}) declared agents; a name
# outside all three is unknown and finalize-plan/apply keep its files
# (PRESERVE, exit 3 on close). Artifacts are path STRINGS only, so
# plan/apply assertions can join and grep them without object-shape
# guesses. Attribution never authorises deletion: every artifact is
# PRESERVE regardless of known/unknown.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
SKILL_DIR="$(cd "$HERE/.." && pwd)"

# Registry lookup order: repo-local cli-profiles.json, then the skill's
# own repo checkout (umputum root), then $HOME fallback.
agent_registry() {
  local repo="${1:-$(pwd)}"
  local ws_root
  ws_root="$(cd "$SKILL_DIR/../.." 2>/dev/null && pwd || echo "")"
  python3 - "$repo" "$ws_root" "$SKILL_DIR/config/defaults.json" <<'PY'
import json, os, sys
repo, ws, cfg_path = sys.argv[1], sys.argv[2], sys.argv[3]
regs = []
seen_files = []
for c in (os.path.join(repo, "cli-profiles.json"),
          os.path.join(ws, "cli-profiles.json") if ws else "",
          os.path.join(os.path.expanduser("~"), "work", "umputum", "cli-profiles.json")):
    if c and os.path.isfile(c):
        try:
            d = json.load(open(c))
            regs = [k for k in d.keys() if not k.startswith("_")]
            seen_files.append(c)
            break
        except Exception:
            continue
try:
    c = json.load(open(cfg_path))
    regs += list(c.get("agents_config_overrides", {}).keys())
    sess_rel = c.get("session_config", ".session/finalizer.json")
except Exception:
    sess_rel = ".session/finalizer.json"
# Repo-local session declaration: extra known names for attribution.
try:
    s = json.load(open(os.path.join(repo, sess_rel)))
    ag = s.get("agents", {})
    if isinstance(ag, dict):
        regs += [k for k in ag.keys() if not k.startswith("_")]
except Exception:
    pass
seen = []
for r in regs:
    if r not in seen:
        seen.append(r)
print("\n".join(seen))
PY
}

agent_is_known() {
  local name="$1" repo="${2:-$(pwd)}"
  agent_registry "$repo" | grep -qx "$name"
}

# Session artifacts: path strings under .pair/** and .session/** (minus
# marker_exempt), plus untracked files under them. Committed-role walks
# need no registry: every agent, known or not, lands here as PRESERVE.
agent_artifacts() {
  local repo="${1:-$(pwd)}"
  python3 - "$repo" "$SKILL_DIR/config/defaults.json" <<'PY'
import fnmatch, json, os, subprocess, sys
repo, cfg_path = sys.argv[1], sys.argv[2]
try:
    cfg = json.load(open(cfg_path))
except Exception:
    cfg = {}
exempt = cfg.get("marker_exempt", [".session/finalized"])
out = []
seen = set()
def keep(rel):
    return not any(fnmatch.fnmatch(rel, p) or rel == p for p in exempt)
try:
    r = subprocess.run(["git", "-C", repo, "ls-files", "--others", "--exclude-standard"],
                       capture_output=True, text=True, timeout=20)
    untracked = set(r.stdout.splitlines()) if r.returncode == 0 else set()
except Exception:
    untracked = set()
for top in (".pair", ".session"):
    base = os.path.join(repo, top)
    if not os.path.isdir(base):
        continue
    for dp, dn, fn in os.walk(base):
        dn[:] = [d for d in dn if d != ".git"]
        for f in fn:
            rel = os.path.relpath(os.path.join(dp, f), repo)
            if keep(rel) and rel not in seen:
                out.append(rel)
                seen.add(rel)
for u in sorted(untracked):
    if (u.startswith(".pair/") or u.startswith(".session/")) and keep(u) and u not in seen:
        out.append(u)
        seen.add(u)
for p in sorted(out):
    print(p)
PY
}

# Close is a no-op note: the orchestrator drains the mailbox via pair
# tools; unknown roles are PRESERVE (exit 3) so apply keeps their files.
agent_close() {
  local role="$1" repo="${2:-$(pwd)}"
  if ! agent_is_known "$role" "$repo"; then echo "PRESERVE unknown agent: $role"; return 3; fi
  echo "close $role: mailbox drained, no writes performed by adapter (orchestrator confirms via pair tools)"
  return 0
}

agent_help() {
  echo "Usage: agent.sh {list|artifacts|close|order} [args]"
  echo "  list [repo]        - registry from cli-profiles.json keys + overrides"
  echo "  artifacts [repo]   - session artifact paths, one per line"
  echo "  close <role>       - no-op note; UNKNOWN role -> PRESERVE (exit 3)"
  echo "  order              - close order from config"
}

case "${1:-}" in
  --help|-h) agent_help; exit 0;;
  list) agent_registry "${2:-$(pwd)}";;
  artifacts) agent_artifacts "${2:-$(pwd)}";;
  close) agent_close "${2:-}" "${3:-$(pwd)}";;
  order) python3 -c 'import json; print("\n".join(json.load(open("'"$SKILL_DIR"'/config/defaults.json")).get("close_order", ["critic","explorer","writer"])))';;
  "") agent_help; exit 1;;
  *) echo "unknown subcommand: $1" >&2; agent_help; exit 1;;
esac
