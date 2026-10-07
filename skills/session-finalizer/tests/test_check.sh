#!/usr/bin/env bash
# test_check.sh — finalize-check: no args, cwd=<repo>, stdout=check.json, read-only.
# Cases: 01 clean, 04 dirty, 05 untracked, 07 absent provider, 07b crash,
# 09 purity, 13 no build files, 14 no remote, --help/exit codes.
set -uo pipefail
TESTDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=lib.sh
source "$TESTDIR/lib.sh"
hermetic_git_env; init_trash; trap trash_cleanup EXIT
NO_PROVIDER_BIN="$(trash "$(clean_bin_without gh glab)")"
NO_PROVIDER_PATH="$NO_PROVIDER_BIN:/usr/bin:/bin"

# --help
bash "$CHECK_BIN" --help >"$(trash "$(mktemp "${TMPDIR}/h.XXXXXX")")" 2>/dev/null
[ "$?" -eq 0 ] && ok "check-help" || fail "check-help" "exit $?"

# --- 01: clean session (merged, pushed -> blocked:false) ---
repo="$(trash "$(make_repo main)")"
git -C "$repo" checkout -qb feature
printf 'feat\n' >"$repo/feat.txt"; git -C "$repo" add feat.txt; git -C "$repo" commit -qm feat
mock_remote "$repo" main >/dev/null
git -C "$repo" push -q -u origin feature
git -C "$repo" checkout -q main; git -C "$repo" merge -q --no-ff feature -m m; git -C "$repo" push -q origin main
git -C "$repo" checkout -q feature
out="$(trash "$(mktemp "${TMPDIR}/c.XXXXXX")")"
run_check "$repo" "$out"
[ "$CAP_EXIT" -eq 0 ] && must_be_json "$out" "01-json" && {
  python3 - "$out" <<'PY' 2>/dev/null || fail "01-clean" "blocked expected false"
import json,sys
d=json.load(open(sys.argv[1]))
assert d["branch"]=="feature", d["branch"]
assert d["unpushed"]==[], d["unpushed"]
assert d["merged_in_base"] is True, d["merged_in_base"]
assert d["blocked"] is False, d
need={"branch","base","git_porcelain","untracked","unpushed","upstream","merged_in_base","pr","todos_in_diff","worktrees","temp_files","session_artifacts","checks","blocked","block_reasons"}
assert need <= set(d), set(d)
assert isinstance(d["session_artifacts"],list) and all(isinstance(x,str) for x in d["session_artifacts"]), d["session_artifacts"]
assert isinstance(d["checks"],list)
PY
  [ "$?" -eq 0 ] && ok "01-clean"
}

# --- 04: dirty worktree -> blocked tree-dirty ---
repo="$(trash "$(make_repo main)")"
printf 'dirty\n' >>"$repo/README.md"
sha_before="$(tree_sha "$repo")"
out="$(trash "$(mktemp "${TMPDIR}/c.XXXXXX")")"
run_check "$repo" "$out"
[ "$CAP_EXIT" -eq 0 ] && must_be_json "$out" "04-json" && {
  python3 - "$out" <<'PY' 2>/dev/null || fail "04-dirty" "expected blocked tree-dirty"
import json,sys
d=json.load(open(sys.argv[1]))
assert d["blocked"] is True, d
assert any("dirty" in r or "porcelain" in r or "tree" in r for r in d["block_reasons"]), d["block_reasons"]
assert len(d["git_porcelain"])>0
PY
  [ "$?" -eq 0 ] && ok "04-dirty"
  [ "$(tree_sha "$repo")" = "$sha_before" ] && ok "04-readonly" || fail "04-readonly" "check mutated tree"
}

# --- 05: untracked listed ---
repo="$(trash "$(make_repo main)")"
printf 'x\n' >"$repo/scratch.tmp"; printf 'y\n' >"$repo/keep.md"
out="$(trash "$(mktemp "${TMPDIR}/c.XXXXXX")")"
run_check "$repo" "$out"
python3 - "$out" scratch.tmp keep.md <<'PY' 2>/dev/null && ok "05-untracked" || fail "05-untracked" "missing untracked"
import json,sys
d=json.load(open(sys.argv[1]))
u=" ".join(d["untracked"])
assert sys.argv[2] in u and sys.argv[3] in u, d["untracked"]
PY

# --- 07: no gh/glab in PATH -> pr UNKNOWN, not blocker by itself ---
repo="$(trash "$(make_repo main)")"
out="$(trash "$(mktemp "${TMPDIR}/c.XXXXXX")")"
( cd "$repo" && env PATH="$NO_PROVIDER_BIN" bash "$CHECK_BIN" >"$out" 2>"$out.err" ); CAP_EXIT=$?
[ "$CAP_EXIT" -eq 0 ] && python3 - "$out" <<'PY' 2>/dev/null && ok "07-absent-unknown" || fail "07-absent-unknown" "pr should be UNKNOWN"
import json,sys
d=json.load(open(sys.argv[1]))
pr=d["pr"]
assert pr=="UNKNOWN" or (isinstance(pr,dict) and "UNKNOWN" in str(pr.values())), pr
PY

# --- 07b: crashing provider -> UNKNOWN ---
repo="$(trash "$(make_repo main)")"
mockbin="$(trash "$(mock_bin crash)")"
out="$(trash "$(mktemp "${TMPDIR}/c.XXXXXX")")"
( cd "$repo" && env PATH="$mockbin:/usr/bin:/bin" bash "$CHECK_BIN" >"$out" 2>"$out.err" ); CAP_EXIT=$?
[ "$CAP_EXIT" -eq 0 ] && python3 - "$out" <<'PY' 2>/dev/null && ok "07b-crash-unknown" || fail "07b-crash-unknown" "crash must yield UNKNOWN"
import json,sys
d=json.load(open(sys.argv[1]))
pr=d["pr"]
assert pr=="UNKNOWN" or (isinstance(pr,dict) and "UNKNOWN" in str(pr.values())), pr
PY

# --- 09: purity byte-identical ---
repo="$(trash "$(make_repo main)")"
git -C "$repo" checkout -qb feature
printf 'x\n' >"$repo/x.txt"; git -C "$repo" add x.txt; git -C "$repo" commit -qm x
sha_before="$(tree_sha "$repo")"; files_before="$(tree_files "$repo")"
out="$(trash "$(mktemp "${TMPDIR}/c.XXXXXX")")"
run_check "$repo" "$out"
[ "$(tree_sha "$repo")" = "$sha_before" ] && [ "$(tree_files "$repo")" = "$files_before" ] && ok "09-purity" || fail "09-purity" "tree changed"

# --- 13: no package.json/Makefile -> commands UNKNOWN not FAIL/blocker ---
repo="$(trash "$(make_repo main)")"
out="$(trash "$(mktemp "${TMPDIR}/c.XXXXXX")")"
run_check "$repo" "$out"
python3 - "$out" <<'PY' 2>/dev/null && ok "13-unknown-cmds" || fail "13-unknown-cmds" "expected UNKNOWN non-blocking"
import json,sys
d=json.load(open(sys.argv[1]))
cs={c["name"]:c for c in d["checks"]}
for n in ("test","lint","build","typecheck"):
    assert n in cs, cs.keys()
    assert cs[n]["status"]=="UNKNOWN", (n,cs[n])
    assert cs[n]["cmd"], n
assert not d["blocked"], d
PY

# --- 14: no remote -> pr UNKNOWN, base UNKNOWN, merged false ---
repo="$(trash "$(make_repo main)")"
[ -z "$(git -C "$repo" remote)" ] || git -C "$repo" remote remove origin
out="$(trash "$(mktemp "${TMPDIR}/c.XXXXXX")")"
run_check "$repo" "$out"
python3 - "$out" <<'PY' 2>/dev/null && ok "14-no-remote" || fail "14-no-remote" "expected UNKNOWN base/pr, merged false"
import json,sys
d=json.load(open(sys.argv[1]))
assert d["base"]=="UNKNOWN", d["base"]
assert d["merged_in_base"] is False, d
pr=d["pr"]
assert pr=="UNKNOWN" or (isinstance(pr,dict) and "UNKNOWN" in str(pr.values())), pr
PY

# --- bad usage -> exit 2 ---
bash "$CHECK_BIN" --bogus >/dev/null 2>&1; [ "$?" -eq 2 ] && ok "check-usage-2" || fail "check-usage-2" "expected exit 2"

tally
