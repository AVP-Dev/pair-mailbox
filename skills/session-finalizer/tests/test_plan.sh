#!/usr/bin/env bash
# test_plan.sh — finalize-plan <check.json> (stdout=plan.json, pure: no git).
# Cases: 02 unpushed PRESERVE, 03 unmerged PRESERVE, 06 merged SAFE,
# 05 temps, 12 protected, detached/UNKNOWN HEAD, worktree+artifacts PRESERVE,
# worktree smoke via real repos, --help.
set -uo pipefail
TESTDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=lib.sh
source "$TESTDIR/lib.sh"
hermetic_git_env; init_trash; trap trash_cleanup EXIT

bash "$PLAN_BIN" --help >/dev/null 2>&1
[ "$?" -eq 0 ] && ok "plan-help" || fail "plan-help" "exit $?"

# purity oracle: plan must not invoke git (trace) and must not touch repo
repo="$(trash "$(make_repo main)")"
sha_before="$(tree_sha "$repo")"
ck="$(trash "$(mktemp "${TMPDIR}/ck.XXXXXX")")"; fixture_check clean "$ck"
pl="$(trash "$(mktemp "${TMPDIR}/pl.XXXXXX")")"
( cd "$repo" && bash "$PLAN_BIN" "$ck" >"$pl" 2>"$pl.err" ); CAP_EXIT=$?
[ "$CAP_EXIT" -eq 0 ] && [ "$(tree_sha "$repo")" = "$sha_before" ] && ok "plan-pure" || fail "plan-pure" "plan touched repo or failed"
# no git binary on PATH at all -> plan must still work
ck2="$(trash "$(mktemp "${TMPDIR}/ck.XXXXXX")")"; fixture_check unmerged "$ck2"
pl2="$(trash "$(mktemp "${TMPDIR}/pl2.XXXXXX")")"
NOGIT_BIN="$(trash "$(clean_bin_without git)")"
env PATH="$NOGIT_BIN" bash "$PLAN_BIN" "$ck2" >"$pl2" 2>"$pl2.err"; [ "$?" -eq 0 ] && ok "plan-no-git" || fail "plan-no-git" "plan needs git : $(cat "$pl2.err")"

check_case() { # <variant> <label> <py-assert-on-plan>
  local v="$1" lbl="$2" code="$3" ck pl
  ck="$(mktemp "${TMPDIR}/ck.XXXXXX")"; trash "$ck" >/dev/null
  pl="$(mktemp "${TMPDIR}/pl.XXXXXX")"; trash "$pl" >/dev/null
  fixture_check "$v" "$ck"
  run_plan "$ck" "$pl"
  [ "$CAP_EXIT" -eq 0 ] || { fail "$lbl" "exit $CAP_EXIT"; return; }
  python3 - "$pl" "$code" <<'PY' 2>/dev/null || fail "$lbl" "assertion failed: $(cat "$pl")"
import json,sys
d=json.load(open(sys.argv[1]))
exec(sys.argv[2])
PY
  [ "$?" -eq 0 ] && ok "$lbl"
}

# 02: unpushed -> branch PRESERVE, blocked copy
check_case unpushed "02-unpushed-preserve" '
assert d["blocked"] is True
b=[a for a in d["actions"] if a["target"]=="feature"]
assert b and b[0]["kind"]=="PRESERVE" and b[0]["reason"], b
'
# 03: unmerged -> branch PRESERVE with all reasons
check_case unmerged "03-unmerged-preserve" '
b=[a for a in d["actions"] if a["target"]=="feature"]
assert b and b[0]["kind"]=="PRESERVE" and b[0]["reason"], b
'
# 06-shape: clean merged -> branch SAFE with recheck_cmd
check_case clean "06-merged-safe-shape" '
b=[a for a in d["actions"] if a["target"]=="feature"]
assert b and b[0]["kind"]=="SAFE" and b[0]["recheck_cmd"], b
'
# 12: protected base branch -> PRESERVE even if merged
ck="$(trash "$(mktemp "${TMPDIR}/ck.XXXXXX")")"
python3 - "$ck" <<'PY'
import json,sys
d={"branch":"main","base":"main","git_porcelain":[],"untracked":[],"unpushed":[],"upstream":"","merged_in_base":True,"pr":"UNKNOWN","todos_in_diff":[],"worktrees":[],"temp_files":[],"session_artifacts":[],"checks":[],"blocked":False,"block_reasons":[]}
json.dump(d,open(sys.argv[1],"w"))
PY
pl="$(trash "$(mktemp "${TMPDIR}/pl.XXXXXX")")"
run_plan "$ck" "$pl"
python3 - "$pl" <<'PY' 2>/dev/null && ok "12-protected-preserve" || fail "12-protected-preserve" "$(cat "$pl")"
import json,sys
d=json.load(open(sys.argv[1]))
b=[a for a in d["actions"] if a["target"]=="main"]
assert b and b[0]["kind"]=="PRESERVE" and b[0]["reason"], b
PY

# detached/UNKNOWN branch -> PRESERVE target HEAD
ck="$(trash "$(mktemp "${TMPDIR}/ck.XXXXXX")")"
python3 - "$ck" <<'PY'
import json
d={"branch":"UNKNOWN","base":"main","git_porcelain":[],"untracked":[],"unpushed":[],"upstream":"","merged_in_base":False,"pr":"UNKNOWN","todos_in_diff":[],"worktrees":[],"temp_files":[],"session_artifacts":[],"checks":[],"blocked":True,"block_reasons":["unknown-branch"]}
json.dump(d,open(__import__("sys").argv[1],"w"))
PY
pl="$(trash "$(mktemp "${TMPDIR}/pl.XXXXXX")")"
run_plan "$ck" "$pl"
python3 - "$pl" <<'PY' 2>/dev/null && ok "plan-unknown-head" || fail "plan-unknown-head" "$(cat "$pl")"
import json,sys
d=json.load(open(sys.argv[1]))
h=[a for a in d["actions"] if a["target"]=="HEAD"]
assert h and h[0]["kind"]=="PRESERVE" and h[0]["reason"], d["actions"]
PY

# worktrees ALWAYS PRESERVE; artifacts ALWAYS PRESERVE (strings)
ck="$(trash "$(mktemp "${TMPDIR}/ck.XXXXXX")")"
python3 - "$ck" <<'PY'
import json,sys
d={"branch":"feature","base":"main","git_porcelain":[],"untracked":[],"unpushed":[],"upstream":"","merged_in_base":True,"pr":"UNKNOWN","todos_in_diff":[],"worktrees":[{"path":"/tmp/wt1","branch":"feature"}],"temp_files":[],"session_artifacts":[".session/mystery-agent/state.json"],"checks":[],"blocked":False,"block_reasons":[]}
json.dump(d,open(sys.argv[1],"w"))
PY
pl="$(trash "$(mktemp "${TMPDIR}/pl.XXXXXX")")"
run_plan "$ck" "$pl"
python3 - "$pl" <<'PY' 2>/dev/null && ok "plan-wt-artifact-preserve" || fail "plan-wt-artifact-preserve" "$(cat "$pl")"
import json,sys
d=json.load(open(sys.argv[1]))
w=[a for a in d["actions"] if a["type"]=="worktree"]
assert w and all(a["kind"]=="PRESERVE" and a["reason"] for a in w), w
f=[a for a in d["actions"] if a["type"]=="artifact"]
assert f and all(a["kind"]=="PRESERVE" and a["reason"] for a in f), f
PY

# 05: temp .tmp SAFE with recheck; non-temp PRESERVE; secrets guard
ck="$(trash "$(mktemp "${TMPDIR}/ck.XXXXXX")")"
python3 - "$ck" <<'PY'
import json,sys
d={"branch":"feature","base":"main","git_porcelain":[],"untracked":["a.tmp","keep.md"],"unpushed":[],"upstream":"","merged_in_base":True,"pr":"UNKNOWN","todos_in_diff":[],"worktrees":[],"temp_files":["a.tmp"],"session_artifacts":[],"checks":[],"blocked":False,"block_reasons":[]}
json.dump(d,open(sys.argv[1],"w"))
PY
pl="$(trash "$(mktemp "${TMPDIR}/pl.XXXXXX")")"
run_plan "$ck" "$pl"
python3 - "$pl" <<'PY' 2>/dev/null && ok "05-temp-safe-shape" || fail "05-temp-safe-shape" "$(cat "$pl")"
import json,sys
d=json.load(open(sys.argv[1]))
t=[a for a in d["actions"] if a["target"]=="a.tmp"]
assert t and t[0]["kind"]=="SAFE" and t[0]["recheck_cmd"], t
PY

# ids A01.. unique; bad input -> exit 2
python3 - "$pl" <<'PY' 2>/dev/null && ok "plan-ids" || fail "plan-ids" "ids malformed"
import json,sys,re
d=json.load(open(sys.argv[1]))
ids=[a["id"] for a in d["actions"]]
assert ids==["A%02d"%(i+1) for i in range(len(ids))], ids
PY
bad="$(trash "$(mktemp "${TMPDIR}/bad.XXXXXX")")"; printf 'nope' >"$bad"
bash "$PLAN_BIN" "$bad" >/dev/null 2>&1; [ "$?" -eq 2 ] && ok "plan-bad-2" || fail "plan-bad-2" "expected exit 2"

tally
