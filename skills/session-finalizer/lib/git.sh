#!/usr/bin/env bash
# git.sh - read-only git helpers. Sourced, never executed for effect.
# Every helper runs a single read-only git plumbing/porcelain command;
# anything that could move HEAD, touch refs, or hit the network without
# a timeout is rejected here (base detection prefers local refs so
# hermetic/offline repos resolve without gh/network).
set -u

# Repo top-level; falls back to cwd when outside a repo.
git_root() { git rev-parse --show-toplevel 2>/dev/null || pwd; }

# Current branch, or UNKNOWN when detached/missing (callers treat
# UNKNOWN as "no branch proof", never as a deletable name).
git_branch() {
  local b
  b=$(git branch --show-current 2>/dev/null) && [ -n "$b" ] && { printf '%s' "$b"; return 0; }
  echo "UNKNOWN"
}

git_head() { git rev-parse HEAD 2>/dev/null || echo "UNKNOWN"; }

# Upstream tracking ref, or the literal string null (JSON needs null,
# not "UNKNOWN", so callers map it explicitly).
git_upstream() {
  local u
  u=$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null) && printf '%s' "$u" || echo "null"
}

git_porcelain() { git status --porcelain=v1 2>/dev/null || echo "UNKNOWN"; }

# Default branch without hardcoding main/master: local origin/HEAD first,
# then `git remote show origin` (bounded: it can hit the network), then
# hosting API, else UNKNOWN. Callers must treat UNKNOWN as unproven.
git_base() {
  local ref target b
  ref=$(git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null) || true
  if [ -n "${ref:-}" ]; then basename "$ref"; return 0; fi
  if git remote get-url origin >/dev/null 2>&1; then
    target=$(timeout 15 git remote show origin 2>/dev/null | awk -F': ' '/HEAD branch/ {print $2}') || true
    if [ -n "${target:-}" ]; then echo "$target"; return 0; fi
  fi
  if command -v gh >/dev/null 2>&1; then
    b=$(timeout 15 gh repo view --json defaultBranchRef --jq '.defaultBranchRef.name' 2>/dev/null) || true
    if [ -n "${b:-}" ]; then echo "$b"; return 0; fi
  fi
  echo "UNKNOWN"
}

# Unpushed commits as "<sha> <subject>" lines, empty when no upstream.
git_unpushed_list() {
  local up
  up=$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null) || return 0
  git log --format='%H %s' "$up"..HEAD 2>/dev/null || true
}

# Merged proof is git evidence only (merge-base ancestry or --merged
# listing); absence of proof prints false, never an error.
git_merged_in_base() {
  local base="$1" br="$2"
  if [ "$base" = "UNKNOWN" ] || [ "$br" = "UNKNOWN" ]; then echo "false"; return 0; fi
  if git merge-base --is-ancestor "$br" "origin/$base" 2>/dev/null; then echo "true"; return 0; fi
  if git merge-base --is-ancestor "$br" "$base" 2>/dev/null; then echo "true"; return 0; fi
  if git branch --merged "$base" 2>/dev/null | grep -Eq "(^|[ *] )$br\$"; then echo "true"; return 0; fi
  if git branch --merged "origin/$base" 2>/dev/null | grep -Eq "(^|[ *] )$br\$"; then echo "true"; return 0; fi
  echo "false"
}

# Protected = fnmatch against protected_branches+protected_patterns from
# the skill config; prints yes/no (config rules, not a hardcoded list).
git_is_protected() {
  local br="$1" cfg="$2"
  python3 - "$br" "$cfg" <<'PY'
import json, sys, fnmatch
br, cfg = sys.argv[1], sys.argv[2]
try:
    d = json.load(open(cfg))
except Exception:
    d = {}
pats = d.get("protected_patterns", []) + d.get("protected_branches", [])
for p in pats:
    if fnmatch.fnmatch(br, p):
        print("yes")
        break
else:
    print("no")
PY
}

if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
  echo "lib/git.sh - read-only git helpers (root/branch/head/upstream/porcelain/base/unpushed/merged/protected)"
  exit 0
fi
