#!/usr/bin/env bash
# test_guards.sh — cross-cutting: agents unknown/config, trunk base,
# failing checks BLOCKED, adapters matrix, grep-gate read-only, real-repo guard.
set -uo pipefail
TESTDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=lib.sh
source "$TESTDIR/lib.sh"
hermetic_git_env; init_trash; trap trash_cleanup EXIT

# --- 11: failing test/build -> BLOCKED, apply short-circuit, tree intact ---
if command -v npm >/dev/null 2>&1; then
  repo="$(trash "$(make_repo main)")"
  cat >"$repo/package.json" <<'EOF'
{"name":"fx","version":"0.0.0","scripts":{"test":"exit 1"}}
EOF
  git -C "$repo" add package.json; git -C "$repo" commit -qm fx
  sha_before="$(tree_sha "$repo")"
  CKFILE="$(trash "$(mktemp "${TMPDIR}/ck.XXXXXX")")"
  ( cd "$repo" && bash "$CHECK_BIN" >"$CKFILE" 2>/dev/null )
  python3 - "$CKFILE" <<'PY' 2>/dev/null && ok "11-fail-blocked" || fail "11-fail-blocked" "$(cat "$CKFILE")"
import json,sys
d=json.load(open(sys.argv[1]))
fails=[c for c in d["checks"] if c["status"]=="FAIL"]
assert fails, d["checks"]
assert d["blocked"] is True, d
assert any("test" in c["name"] for c in fails), fails
PY
  pl="$(trash "$(mktemp "${TMPDIR}/pl.XXXXXX")")"
  bash "$PLAN_BIN" "$CKFILE" >"$pl" 2>/dev/null
  ap="$(trash "$(mktemp "${TMPDIR}/ap.XXXXXX")")"
  run_apply "$repo" "$pl" "$ap" --yes
  python3 - "$ap" <<'PY' && [ "$(tree_sha "$repo")" = "$sha_before" ] && ok "11-short-circuit" || fail "11-short-circuit" "$(cat "$ap")"
import json,sys
d=json.load(open(sys.argv[1]))
assert d["applied"]==[] and d["blocked"] is True, d
PY
else
  fail "11-fail-blocked" "npm not available"
fi

# --- 15: unknown agent artifacts PRESERVE ---
repo="$(trash "$(make_repo main)")"
mkdir -p "$repo/.session/mystery-agent"
printf '{"ctx":1}\n' >"$repo/.session/mystery-agent/state.json"
CKFILE="$(trash "$(mktemp "${TMPDIR}/ck.XXXXXX")")"
( cd "$repo" && bash "$CHECK_BIN" >"$CKFILE" 2>/dev/null )
python3 - "$CKFILE" <<'PY' 2>/dev/null && ok "15-listed" || fail "15-listed" "$(cat "$CKFILE")"
import json,sys
d=json.load(open(sys.argv[1]))
assert any("mystery-agent" in s for s in d["session_artifacts"]), d["session_artifacts"]
PY
pl="$(trash "$(mktemp "${TMPDIR}/pl.XXXXXX")")"
bash "$PLAN_BIN" "$CKFILE" >"$pl" 2>/dev/null
python3 - "$pl" <<'PY' 2>/dev/null && ok "15-preserve" || fail "15-preserve" "$(cat "$pl")"
import json,sys
d=json.load(open(sys.argv[1]))
m=[a for a in d["actions"] if "mystery-agent" in a["target"]]
assert m and all(a["kind"]=="PRESERVE" and a["reason"] for a in m), m
assert not [a for a in m if a["kind"]=="SAFE"], m
PY

# --- 16: new agent via config, no core edits; attributed but PRESERVE ---
repo="$(trash "$(make_repo main)")"
mkdir -p "$repo/.session/nova-agent"
printf '{"ctx":1}\n' >"$repo/.session/nova-agent/context.json"
cat >"$repo/.session/finalizer.json" <<'EOF'
{"agents": {"nova": {"globs": [".session/nova-agent/**"]}}}
EOF
git -C "$repo" add .session/finalizer.json; git -C "$repo" commit -qm nova 2>/dev/null || true
CKFILE="$(trash "$(mktemp "${TMPDIR}/ck.XXXXXX")")"
( cd "$repo" && bash "$CHECK_BIN" >"$CKFILE" 2>/dev/null )
python3 - "$CKFILE" <<'PY' 2>/dev/null && ok "16-listed" || fail "16-listed" "$(cat "$CKFILE")"
import json,sys
d=json.load(open(sys.argv[1]))
assert any("nova-agent" in s for s in d["session_artifacts"]), d["session_artifacts"]
PY
# --- grep-gate: finalize-check has no write verbs (word-boundary scan;
# read-only queries like merge-base / branch --merged / merged_in_base are fine) ---
if grep -nE '(^|[^a-zA-Z_.-])(git +(checkout|push|commit( |$)|merge( |$)|branch +-d|worktree +(add|remove))|(rm|unlink)( +|$))|os\.remove' "$CHECK_BIN" >/dev/null; then
  fail "grep-gate" "$(grep -nE '(^|[^a-zA-Z_.-])(git +(checkout|push|commit( |$)|merge( |$)|branch +-d|worktree +(add|remove))|(rm|unlink)( +|$))|os\.remove' "$CHECK_BIN")"
else
  ok "grep-gate"
fi
# scripts use python3 stdlib only (a bare `jq` binary call; gh's --jq flag is fine)
if grep -rnE '(^|[^a-zA-Z_-])jq( |$)' "$SKILL_ROOT/scripts" "$SKILL_ROOT/lib" "$SKILL_ROOT/adapters" 2>/dev/null | grep -v '\-\-jq' | grep -vE '^[^:]+:[0-9]+:#' | grep -q .; then
  fail "no-jq" "jq binary used in scripts"
else
  ok "no-jq"
fi
git -C "$repo" checkout -qb trunk 2>/dev/null || git -C "$repo" checkout -q trunk
mock_remote "$repo" trunk >/dev/null
git -C "$repo" checkout -qb feature
printf 'w\n' >"$repo/w.txt"; git -C "$repo" add w.txt; git -C "$repo" commit -qm w
git -C "$repo" checkout -q trunk; git -C "$repo" merge -q --no-ff feature -m m
git -C "$repo" checkout -q feature
CKFILE="$(trash "$(mktemp "${TMPDIR}/ck.XXXXXX")")"
( cd "$repo" && bash "$CHECK_BIN" >"$CKFILE" 2>/dev/null )
python3 - "$CKFILE" <<'PY' 2>/dev/null && ok "17-trunk" || fail "17-trunk" "$(cat "$CKFILE")"
import json,sys
d=json.load(open(sys.argv[1]))
assert d["base"]=="trunk", d["base"]
assert d["merged_in_base"] is True, d
PY

# --- adapters: hosting base/pr + commands matrix smoke ---
bash "$SKILL_ROOT/adapters/hosting.sh" --help >/dev/null 2>&1
[ "$?" -eq 0 ] && ok "hosting-help" || fail "hosting-help" "exit $?"
bash "$SKILL_ROOT/adapters/commands.sh" --help >/dev/null 2>&1
[ "$?" -eq 0 ] && ok "commands-help" || fail "commands-help" "exit $?"
bash "$SKILL_ROOT/adapters/agent.sh" --help >/dev/null 2>&1
[ "$?" -eq 0 ] && ok "agent-help" || fail "agent-help" "exit $?"
# unknown agent exit 3 on close-query (contract: unknown->PRESERVE exit 3)
bash "$SKILL_ROOT/adapters/agent.sh" close mystery-agent >/dev/null 2>&1
[ "$?" -eq 3 ] && ok "agent-unknown-3" || fail "agent-unknown-3" "expected exit 3"



# --- B1 e2e: clean origin-backed repo -> FINALIZED + marker ---
repo="$(trash "$(make_repo main)")"
mock_remote "$repo" main >/dev/null
CKFILE="$(trash "$(mktemp "${TMPDIR}/ck.XXXXXX")")"
( cd "$repo" && bash "$CHECK_BIN" >"$CKFILE" 2>/dev/null )
python3 - "$CKFILE" <<'PY' 2>/dev/null && ok "B1-check-unblocked" || fail "B1-check-unblocked" "$(cat "$CKFILE")"
import json,sys
d=json.load(open(sys.argv[1]))
assert d["blocked"] is False, d["block_reasons"]
PY
pl="$(trash "$(mktemp "${TMPDIR}/pl.XXXXXX")")"
bash "$PLAN_BIN" "$CKFILE" >"$pl" 2>/dev/null
python3 - "$pl" "$repo" <<'PY' 2>/dev/null && ok "B1-no-self-worktree" || fail "B1-no-self-worktree" "$(cat "$pl")"
import json,sys,os
d=json.load(open(sys.argv[1])); repo=os.path.realpath(sys.argv[2])
wt=[a for a in d["actions"] if a["type"]=="worktree"]
assert all(os.path.realpath(a["target"]) != repo for a in wt), wt
PY
ap="$(trash "$(mktemp "${TMPDIR}/ap.XXXXXX")")"
( cd "$repo" && bash "$APPLY_BIN" "$pl" --yes >"$ap" 2>"$ap.err" )
python3 - "$ap" <<'PY' 2>/dev/null && ok "B1-apply-unblocked" || fail "B1-apply-unblocked" "$(cat "$ap")"
import json,sys
d=json.load(open(sys.argv[1]))
assert d["blocked"] is False, d
PY
out="$(trash "$(mktemp "${TMPDIR}/r.XXXXXX")")"
( cd "$repo" && bash "$REPORT_BIN" "$CKFILE" "$ap" "$pl" >"$out" 2>"$out.err" )
head -1 "$out" | grep -q "SESSION FINALIZED" && ok "B1-finalized" || fail "B1-finalized" "$(head -3 "$out")"
[ -f "$repo/.session/finalized" ] && ok "B1-marker" || fail "B1-marker" "marker missing"

# --- B2: bad JSON -> exit 2 (apply + report), no traceback-as-1 ---
bad="$(trash "$(mktemp "${TMPDIR}/bad.XXXXXX")")"; printf 'not json{{' >"$bad"
repo2="$(trash "$(make_repo main)")"
( cd "$repo2" && bash "$APPLY_BIN" "$bad" --yes >"$bad.out" 2>"$bad.err" ); [ "$?" -eq 2 ] && ok "B2-apply-2" || fail "B2-apply-2" "exit $?"
ckok="$(trash "$(mktemp "${TMPDIR}/ck.XXXXXX")")"; printf '{"blocked":false}' >"$ckok"
( cd "$repo2" && bash "$REPORT_BIN" "$bad" "$ckok" >"$bad.rout" 2>"$bad.rerr" ); [ "$?" -eq 2 ] && ok "B2-report-2" || fail "B2-report-2" "exit $?"

# --- slots from config: check output mirrors defaults checks_slots ---
CKFILE="$(trash "$(mktemp "${TMPDIR}/ck.XXXXXX")")"
repo3="$(trash "$(make_repo main)")"
( cd "$repo3" && bash "$CHECK_BIN" >"$CKFILE" 2>/dev/null )
python3 - "$SKILL_ROOT/config/defaults.json" "$CKFILE" <<'PY' 2>/dev/null && ok "slots-from-config" || fail "slots-from-config" "slots not config-driven"
import json,sys
cfg=json.load(open(sys.argv[1])); d=json.load(open(sys.argv[2]))
names=[c["name"] for c in d["checks"]]
assert names[:2]==["git-status","git-diff-check"], names
assert names[2:]==cfg["checks_slots"], (names, cfg["checks_slots"])
PY

# --- MEDIUM: no-upstream unpushed visible via origin/<branch> fallback ---
repo="$(trash "$(make_repo main)")"
mock_remote "$repo" main >/dev/null
git -C "$repo" checkout -qb feature
printf 'f\n' >"$repo/f.txt"; git -C "$repo" add f.txt; git -C "$repo" commit -qm f
git -C "$repo" push -q origin feature
printf 'g\n' >>"$repo/f.txt"; git -C "$repo" commit -qam g
git -C "$repo" branch --unset-upstream 2>/dev/null || git -C "$repo" branch --unset-upstream feature 2>/dev/null || true
CKFILE="$(trash "$(mktemp "${TMPDIR}/ck.XXXXXX")")"
( cd "$repo" && bash "$CHECK_BIN" >"$CKFILE" 2>/dev/null )
python3 - "$CKFILE" <<'PY' 2>/dev/null && ok "MEDIUM-no-upstream-seen" || fail "MEDIUM-no-upstream-seen" "$(cat "$CKFILE")"
import json,sys
d=json.load(open(sys.argv[1]))
assert len(d["unpushed"])==1, d["unpushed"]
assert d["unpushed"][0]["subject"]=="g", d["unpushed"]
assert d["blocked"], d["block_reasons"]
PY
# --- MEDIUM: protected branch without origin is normal local state, not UNKNOWN ---
repo="$(trash "$(make_repo main)")"
CKFILE="$(trash "$(mktemp "${TMPDIR}/ck.XXXXXX")")"
( cd "$repo" && bash "$CHECK_BIN" >"$CKFILE" 2>/dev/null )
python3 - "$CKFILE" <<'PY' 2>/dev/null && ok "MEDIUM-protected-local-clean" || fail "MEDIUM-protected-local-clean" "$(cat "$CKFILE")"
import json,sys
d=json.load(open(sys.argv[1]))
assert d["unpushed_unknown"] is False, d
assert not d["blocked"], d["block_reasons"]
PY

tally
