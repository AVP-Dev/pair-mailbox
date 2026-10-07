#!/usr/bin/env bash
# lib.sh — shared hermetic helpers for session-finalizer acceptance.
# Sourced by tests/test_*.sh. Not a test itself.
#
# Contract interface under test:
#   finalize-check            (cwd=<repo>, no args, stdout=check.json)
#   finalize-plan <check.json>              (stdout=plan.json, pure fn)
#   finalize-apply <plan.json> [--yes]      (cwd=<repo>, stdout=apply.json)
#   finalize-report <check> <apply> [plan]  (stdout=text)
# Every script: --help/-h -> stdout, exit 0.
# Exit codes: 0 ok (incl. blocked:true), 2 usage/bad input, 1 internal.
set -u

SKILL_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
CHECK_BIN="$SKILL_ROOT/scripts/finalize-check"
PLAN_BIN="$SKILL_ROOT/scripts/finalize-plan"
APPLY_BIN="$SKILL_ROOT/scripts/finalize-apply"
REPORT_BIN="$SKILL_ROOT/scripts/finalize-report"

T_PASS=0
T_FAIL=0
ok()   { T_PASS=$((T_PASS + 1)); printf 'PASS %s\n' "$1"; }
fail() { T_FAIL=$((T_FAIL + 1)); printf 'FAIL %s\n%s\n' "$1" "${2:-}" >&2; return 1; }
TMPDIR="${TMPDIR:-/tmp}"
export TMPDIR
hermetic_git_env() {
  if [ ! -d "${TMPDIR}" ]; then
    printf 'ABORT: TMPDIR missing or not a directory\n' >&2
    exit 1
  fi
  TEST_HOME="$(mktemp -d "${TMPDIR}/finalize-home.XXXXXX")" || { printf 'ABORT: mktemp failed\n' >&2; exit 1; }
  export HOME="$TEST_HOME"
  export GIT_CONFIG_GLOBAL="$TEST_HOME/.gitconfig"
  export GIT_CONFIG_SYSTEM=/dev/null
  export GIT_CONFIG_NOSYSTEM=1
  export GIT_TERMINAL_PROMPT=0
  export GIT_EDITOR=true
}

TRASHFILE=""
trash() { printf '%s\n' "$1" >>"$TRASHFILE"; printf '%s' "$1"; }
trash_cleanup() {
  if [ -n "$TRASHFILE" ] && [ -f "$TRASHFILE" ]; then
    grep -v '^$' "$TRASHFILE" | tr '\n' '\0' | xargs -0 -r rm -rf
    rm -f "$TRASHFILE"
  fi
  return 0
}
init_trash() {
  TRASHFILE="$(mktemp "${TMPDIR}/finalize-trash.XXXXXX")" || { printf 'ABORT: TMPDIR mktemp failed\n' >&2; exit 1; }
}

# --- temp repo with one commit on <branch> (default main). No remotes.
make_repo() {
  local branch="${1:-main}" d
  d="$(mktemp -d "${TMPDIR}/finalize-repo.XXXXXX")" || { printf 'ABORT: mktemp repo failed\n' >&2; exit 1; }
  git init -q -b "$branch" "$d" &&
    git -C "$d" config user.email "test@example.local" &&
    git -C "$d" config user.name "Finalize Test" &&
    git -C "$d" config commit.gpgsign false &&
    printf '# fixture\n' >"$d/README.md" &&
    git -C "$d" add README.md &&
    git -C "$d" commit -qm init &&
    printf '%s' "$d"
}

# --- mock-remote: bare repo as origin, push base, pin origin/HEAD.
mock_remote() {
  local repo="$1" base="${2:-main}" bare="$1.origin-bare"
  git init -q --bare "$bare" &&
    git -C "$repo" remote add origin "$bare" &&
    git -C "$repo" push -q origin "$base" &&
    git -C "$repo" fetch -q origin &&
    git -C "$repo" symbolic-ref "refs/remotes/origin/HEAD" "refs/remotes/origin/$base"
}

# --- tree_sha oracle: sha over all files except .git (names+contents).
tree_sha() {
  find "$1" -path "$1/.git" -prune -o -type f -print0 |
    sort -z | xargs -0 sha256sum 2>/dev/null | sha256sum | cut -d' ' -f1
}
# file list (excluding .git) for byte-identity proof.
tree_files() { find "$1" -path "$1/.git" -prune -o -type f -print | sort; }

# --- PATH mocks: dir with fake gh/glab. mode: ok | crash | (absent via path_without).
mock_bin() {
  local mode="$1" d
  d="$(mktemp -d "${TMPDIR}/finalize-mockbin.XXXXXX")"
  for t in gh glab; do
    if [ "$mode" = "ok" ]; then
      printf '#!/bin/sh\necho "{}"\nexit 0\n' >"$d/$t"
    else
      printf '#!/bin/sh\necho "mock %s crash" >&2\nexit 1\n' "$t" >"$d/$t"
    fi
    chmod +x "$d/$t"
  done
  printf '%s' "$d"
}
# PATH dir with symlinks to everything EXCEPT named tools (keeps
# bash/git/python3 working while gh/glab vanish).
clean_bin_without() {
  local d
  d="$(mktemp -d "${TMPDIR}/finalize-cleanbin.XXXXXX")" || { printf 'ABORT: mktemp cleanbin failed\n' >&2; exit 1; }
  local sv="$IFS"; IFS=:
  for dir in $PATH; do
    for f in "$dir"/*; do
      [ -x "$f" ] || continue
      local b="${f##*/}" skip=0
      for n in "$@"; do [ "$b" = "$n" ] && skip=1 && break; done
      [ "$skip" = 1 ] && continue
      [ -e "$d/$b" ] || ln -s "$f" "$d/$b" 2>/dev/null || true
    done
  done
  IFS="$sv"
  printf '%s' "$d"
}
# PATH with every dir containing any named tool removed.
path_without() {
  local d n keep out="" sv="$IFS"; IFS=:
  for d in $PATH; do
    keep=1
    for n in "$@"; do [ -x "$d/$n" ] && keep=0 && break; done
    [ "$keep" = 1 ] && out="${out:+$out:}$d"
  done
  IFS="$sv"
  printf '%s' "$out"
}

# --- runners: capture stdout to file; set CAP_EXIT. Always return 0.
CAP_EXIT=0
run_check() { # <repo> <out> [extra args...]
  local repo="$1" out="$2"; shift 2
  ( cd "$repo" && bash "$CHECK_BIN" "$@" >"$out" 2>"$out.err" ); CAP_EXIT=$?
  return 0
}
run_plan() { # <check.json> <plan-out>
  bash "$PLAN_BIN" "$1" >"$2" 2>"$2.err"; CAP_EXIT=$?
  return 0
}
run_apply() { # <repo> <plan.json> <apply-out> [extra...]
  local repo="$1" plan="$2" out="$3"; shift 3
  ( cd "$repo" && bash "$APPLY_BIN" "$plan" "$@" >"$out" 2>"$out.err" ); CAP_EXIT=$?
  return 0
}
run_report() { # <repo> <check> <apply> [plan] -- <out>
  local repo="$1" ck="$2" ap="$3" pl="${4:-}" out="$5"
  if [ -n "$pl" ]; then
    ( cd "$repo" && bash "$REPORT_BIN" "$ck" "$ap" "$pl" >"$out" 2>"$out.err" ); CAP_EXIT=$?
  else
    ( cd "$repo" && bash "$REPORT_BIN" "$ck" "$ap" >"$out" 2>"$out.err" ); CAP_EXIT=$?
  fi
  return 0
}

# --- assert helpers (python3 stdlib only, no jq in tests' assertions path is fine but jq allowed here).
assert_exit() { # <expected> <label>
  if [ "$CAP_EXIT" = "$1" ]; then ok "$2 (exit $1)"; else fail "$2" "expected exit $1, got $CAP_EXIT"; fi
}
json_eq() { # <file> <py-expr-on-d> <expected> <label>  (py-expr e.g. d['blocked'])
  local got
  got="$(python3 -c "import json;d=json.load(open('$1'));print($2)" 2>/dev/null)" || { fail "$4" "not valid json: $1"; return; }
  if [ "$got" = "$3" ]; then ok "$4"; else fail "$4" "expected [$3] got [$got]"; fi
}
must_be_json() { python3 -c "import json;json.load(open('$1'))" 2>/dev/null && return 0 || { fail "$2" "not valid JSON: $1"; return 1; }; }

# --- fixture_check: synthetic check.json variant for plan/apply unit use.
fixture_check() { # <variant> <out>
  python3 - "$1" "$2" <<'PY'
import json,sys
v, out = sys.argv[1], sys.argv[2]
base = {"branch":"feature","base":"main","git_porcelain":[],"untracked":[],
 "unpushed":[],"upstream":"refs/remotes/origin/main","merged_in_base":True,
 "pr":"UNKNOWN","todos_in_diff":[],"worktrees":[],"temp_files":[],
 "session_artifacts":[],"checks":[],"blocked":False,"block_reasons":[]}
if v == "unpushed":
    base["unpushed"]=[{"sha":"deadbee01","subject":"unpushed work"}]
    base["merged_in_base"]=False; base["blocked"]=True; base["block_reasons"]=["unpushed"]
elif v == "unmerged":
    base["merged_in_base"]=False; base["blocked"]=True; base["block_reasons"]=["unmerged"]
elif v == "dirty":
    base["git_porcelain"]=[" M README.md"]; base["blocked"]=True; base["block_reasons"]=["tree-dirty"]
elif v == "trunk":
    base["base"]="trunk"; base["upstream"]="refs/remotes/origin/trunk"
json.dump(base, open(out,"w"), indent=1)
PY
}

tally() {
  printf 'TALLY pass=%d fail=%d\n' "$T_PASS" "$T_FAIL"
  [ "$T_FAIL" -eq 0 ]
}
