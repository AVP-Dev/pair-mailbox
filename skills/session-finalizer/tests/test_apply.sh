#!/usr/bin/env bash
# test_apply.sh — finalize-apply <plan.json> [--yes] (cwd=<repo>, stdout=apply.json).
# Cases: 02 unpushed no-delete, 03 unmerged + adversarial SAFE, 04 short-circuit,
# 05 temp SAFE deleted / non-temp PRESERVE, 06 merged SAFE deleted w/ evidence,
# 08 marker fast-path, 10 PRESERVE w/ --yes, 12 protected adversarial, dry-run, --force absent.
set -uo pipefail
TESTDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=lib.sh
source "$TESTDIR/lib.sh"
hermetic_git_env; init_trash; trap trash_cleanup EXIT

bash "$APPLY_BIN" --help >/dev/null 2>&1
[ "$?" -eq 0 ] && ok "apply-help" || fail "apply-help" "exit $?"

mkplan() { # <check.json> -> prints plan path via $MKPLAN
  MKPLAN="$(mktemp "${TMPDIR}/pl.XXXXXX")"; trash "$MKPLAN" >/dev/null
  bash "$PLAN_BIN" "$1" >"$MKPLAN" 2>/dev/null || { fail "mkplan" "plan failed"; return 1; }
}
adv_safe_branch() { # <branch> <outfile> : adversarial SAFE plan
  python3 - "$1" "$2" <<'PY'
import json,sys
br, out = sys.argv[1], sys.argv[2]
json.dump({"blocked":False,"block_reasons":[],"actions":[
 {"id":"A01","type":"branch","target":br,"kind":"SAFE","reason":"adversarial","recheck_cmd":"true"}]}, open(out,"w"))
PY
}

# --- 02: unpushed -> blocked, --yes deletes nothing ---
repo="$(trash "$(make_repo main)")"
git -C "$repo" checkout -qb feature
printf 'b\n' >"$repo/b.txt"; git -C "$repo" add b.txt; git -C "$repo" commit -qm base
mock_remote "$repo" main >/dev/null; git -C "$repo" push -q -u origin feature
printf 'ahead\n' >"$repo/a.txt"; git -C "$repo" add a.txt; git -C "$repo" commit -qm ahead
sha_before="$(tree_sha "$repo")"
ck="$(trash "$(mktemp "${TMPDIR}/ck.XXXXXX")")"
( cd "$repo" && bash "$CHECK_BIN" >"$ck" 2>/dev/null )
mkplan "$ck"; ap="$(trash "$(mktemp "${TMPDIR}/ap.XXXXXX")")"
run_apply "$repo" "$MKPLAN" "$ap" --yes
python3 - "$ap" <<'PY' 2>/dev/null && ok "02-blocked-no-delete" || fail "02-blocked-no-delete" "$(cat "$ap")"
import json,sys
d=json.load(open(sys.argv[1]))
assert d["blocked"] is True, d
assert d["applied"]==[] and all(isinstance(s,str) for s in d["skipped"]), d
PY
[ "$(tree_sha "$repo")" = "$sha_before" ] && ok "02-tree-intact" || fail "02-tree-intact" "tree changed"

# --- 03: unmerged real plan + adversarial SAFE both refused ---
repo="$(trash "$(make_repo main)")"
git -C "$repo" checkout -qb feature
printf 'w\n' >"$repo/w.txt"; git -C "$repo" add w.txt; git -C "$repo" commit -qm w
mock_remote "$repo" main >/dev/null
ck="$(trash "$(mktemp "${TMPDIR}/ck.XXXXXX")")"
( cd "$repo" && bash "$CHECK_BIN" >"$ck" 2>/dev/null )
mkplan "$ck"; ap="$(trash "$(mktemp "${TMPDIR}/ap.XXXXXX")")"
run_apply "$repo" "$MKPLAN" "$ap" --yes
git -C "$repo" show-ref --verify -q refs/heads/feature && python3 - "$ap" <<'PY' 2>/dev/null && ok "03-unmerged-refused" || fail "03-unmerged-refused" "$(cat "$ap")"
import json,sys
d=json.load(open(sys.argv[1]))
assert d["applied"]==[] and d["blocked"] is True, d
PY
adv="$(trash "$(mktemp "${TMPDIR}/adv.XXXXXX")")"; adv_safe_branch feature "$adv"
ap2="$(trash "$(mktemp "${TMPDIR}/ap.XXXXXX")")"
run_apply "$repo" "$adv" "$ap2" --yes
git -C "$repo" show-ref --verify -q refs/heads/feature && python3 - "$ap2" <<'PY' 2>/dev/null && ok "03-adversarial-refused" || fail "03-adversarial-refused" "$(cat "$ap2")"
import json,sys
d=json.load(open(sys.argv[1]))
assert d["applied"]==[] and d["blocked"] is True, d
assert d["details"], d
PY

# --- 04: dirty short-circuit ---
repo="$(trash "$(make_repo main)")"
printf 'dirty\n' >>"$repo/README.md"
sha_before="$(tree_sha "$repo")"
ck="$(trash "$(mktemp "${TMPDIR}/ck.XXXXXX")")"
( cd "$repo" && bash "$CHECK_BIN" >"$ck" 2>/dev/null )
mkplan "$ck"; ap="$(trash "$(mktemp "${TMPDIR}/ap.XXXXXX")")"
run_apply "$repo" "$MKPLAN" "$ap" --yes
python3 - "$ap" <<'PY' && [ "$(tree_sha "$repo")" = "$sha_before" ] && ok "04-short-circuit" || fail "04-short-circuit" "$(cat "$ap")"
import json,sys
d=json.load(open(sys.argv[1]))
assert d["applied"]==[] and d["blocked"] is True, d
PY

# --- 05: temp SAFE deleted; non-temp PRESERVE ---
repo="$(trash "$(make_repo main)")"
printf 's\n' >"$repo/scratch.tmp"; printf 'k\n' >"$repo/keep.md"
ck="$(trash "$(mktemp "${TMPDIR}/ck.XXXXXX")")"
( cd "$repo" && bash "$CHECK_BIN" >"$ck" 2>/dev/null )
mkplan "$ck"; ap="$(trash "$(mktemp "${TMPDIR}/ap.XXXXXX")")"
git -C "$repo" checkout -q main 2>/dev/null || true
run_apply "$repo" "$MKPLAN" "$ap" --yes
python3 - "$ap" "$repo" <<'PY' 2>/dev/null && ok "05-temp-deleted" || fail "05-temp-deleted" "$(cat "$ap")"
import json,sys,os
d=json.load(open(sys.argv[1])); repo=sys.argv[2]
assert "scratch.tmp" in d["applied"], d
assert not os.path.exists(os.path.join(repo,"scratch.tmp"))
assert os.path.exists(os.path.join(repo,"keep.md"))
assert all(isinstance(x,str) for x in d["applied"]+d["skipped"]), d
assert d["details"], d
PY

# --- 06: merged SAFE deleted via git branch -d with evidence ---
repo="$(trash "$(make_repo main)")"
git -C "$repo" checkout -qb feature
printf 'f\n' >"$repo/f.txt"; git -C "$repo" add f.txt; git -C "$repo" commit -qm f
mock_remote "$repo" main >/dev/null
git -C "$repo" checkout -q main; git -C "$repo" merge -q --no-ff feature -m m
git -C "$repo" checkout -q main
ck="$(trash "$(mktemp "${TMPDIR}/ck.XXXXXX")")"
( cd "$repo" && bash "$CHECK_BIN" >"$ck" 2>/dev/null )
adv="$(trash "$(mktemp "${TMPDIR}/adv.XXXXXX")")"
python3 - "$ck" "$adv" <<'PY'
import json,sys
ck, out = sys.argv[1], sys.argv[2]
d=json.load(open(ck))
base=d["base"]
json.dump({"blocked":False,"block_reasons":[],"actions":[
 {"id":"A01","type":"branch","target":"feature","kind":"SAFE","reason":"merged","recheck_cmd":"git merge-base --is-ancestor feature %s"%base}]}, open(out,"w"))
PY
ap="$(trash "$(mktemp "${TMPDIR}/ap.XXXXXX")")"
run_apply "$repo" "$adv" "$ap" --yes
! git -C "$repo" show-ref --verify -q refs/heads/feature && python3 - "$ap" <<'PY' 2>/dev/null && ok "06-merged-deleted" || fail "06-merged-deleted" "$(cat "$ap")"
import json,sys
d=json.load(open(sys.argv[1]))
assert "feature" in d["applied"], d
assert any(len(x.get("evidence",""))>0 for x in d["details"] if x.get("outcome")=="applied"), d
PY

# --- 08: marker fast-path (marker.head==HEAD -> Nothing to clean) ---
repo="$(trash "$(make_repo main)")"
ck="$(trash "$(mktemp "${TMPDIR}/ck.XXXXXX")")"
( cd "$repo" && bash "$CHECK_BIN" >"$ck" 2>/dev/null )
mkplan "$ck"
head_now="$(git -C "$repo" rev-parse HEAD)"
mkdir -p "$repo/.session"
python3 - "$repo/.session/finalized" "$head_now" <<'PY'
import json,sys,datetime
json.dump({"head":sys.argv[2],"session":"s1","closed_at":datetime.datetime.now(datetime.timezone.utc).isoformat()}, open(sys.argv[1],"w"))
PY
sha_before="$(tree_sha "$repo")"
ap="$(trash "$(mktemp "${TMPDIR}/ap.XXXXXX")")"
run_apply "$repo" "$MKPLAN" "$ap" --yes
grep -qi "Nothing to clean" "$ap.err" && python3 - "$ap" <<'PY' && [ "$(tree_sha "$repo")" = "$sha_before" ] && ok "08-fast-path" || fail "08-fast-path" "$(cat "$ap") // $(cat "$ap.err")"
import json,sys
d=json.load(open(sys.argv[1]))
assert d["applied"]==[] and d["skipped"]==[] and d["blocked"] is False, d
PY

# --- 10: PRESERVE stays skipped with --yes ---
repo="$(trash "$(make_repo main)")"
git -C "$repo" checkout -qb feature
printf 'w\n' >"$repo/w.txt"; git -C "$repo" add w.txt; git -C "$repo" commit -qm w
mock_remote "$repo" main >/dev/null
ck="$(trash "$(mktemp "${TMPDIR}/ck.XXXXXX")")"
( cd "$repo" && bash "$CHECK_BIN" >"$ck" 2>/dev/null )
mkplan "$ck"; ap="$(trash "$(mktemp "${TMPDIR}/ap.XXXXXX")")"
run_apply "$repo" "$MKPLAN" "$ap" --yes
git -C "$repo" show-ref --verify -q refs/heads/feature && python3 - "$ap" <<'PY' 2>/dev/null && ok "10-preserve-skipped" || fail "10-preserve-skipped" "$(cat "$ap")"
import json,sys
d=json.load(open(sys.argv[1]))
assert d["applied"]==[] and len(d["skipped"])>0, d
PY

# --- 12: adversarial SAFE on main refused ---
repo="$(trash "$(make_repo main)")"
ck="$(trash "$(mktemp "${TMPDIR}/ck.XXXXXX")")"
( cd "$repo" && bash "$CHECK_BIN" >"$ck" 2>/dev/null )
adv="$(trash "$(mktemp "${TMPDIR}/adv.XXXXXX")")"; adv_safe_branch main "$adv"
ap="$(trash "$(mktemp "${TMPDIR}/ap.XXXXXX")")"
run_apply "$repo" "$adv" "$ap" --yes
git -C "$repo" show-ref --verify -q refs/heads/main && python3 - "$ap" <<'PY' 2>/dev/null && ok "12-main-refused" || fail "12-main-refused" "$(cat "$ap")"
import json,sys
d=json.load(open(sys.argv[1]))
assert d["applied"]==[] and d["blocked"] is True, d
PY

# --- dry-run: no --yes deletes nothing, skips all SAFE, blocked from plan ---
repo="$(trash "$(make_repo main)")"
printf 's\n' >"$repo/dry.tmp"
ck="$(trash "$(mktemp "${TMPDIR}/ck.XXXXXX")")"
( cd "$repo" && bash "$CHECK_BIN" >"$ck" 2>/dev/null )
mkplan "$ck"; ap="$(trash "$(mktemp "${TMPDIR}/ap.XXXXXX")")"
run_apply "$repo" "$MKPLAN" "$ap"
python3 - "$ap" "$repo" <<'PY' 2>/dev/null && ok "dry-run" || fail "dry-run" "$(cat "$ap")"
import json,sys,os
d=json.load(open(sys.argv[1]))
assert d["applied"]==[], d
assert os.path.exists(os.path.join(sys.argv[2],"dry.tmp")), d
PY

# --- --force does not exist ---
bash "$APPLY_BIN" --help 2>&1 | grep -q -- "--force" && fail "no-force" "--force must not exist" || ok "no-force"


# --- SEC-A: adversarial recheck_cmd never executes (RCE regression) ---
repo="$(trash "$(make_repo main)")"
mock_remote "$repo" main >/dev/null
git -C "$repo" checkout -qb feat
printf 'w\n' >"$repo/w.txt"; git -C "$repo" add w.txt; git -C "$repo" commit -qm w
git -C "$repo" checkout -q main; git -C "$repo" merge -q --no-ff feat -m m
git -C "$repo" checkout -q main
adv="$(trash "$(mktemp "${TMPDIR}/adv.XXXXXX")")"
python3 - "$adv" <<'PY'
import json,sys
json.dump({"blocked":False,"block_reasons":[],"base":"main","branch":"main","actions":[
 {"id":"A01","type":"file","target":"pwned.txt","target_name":"pwned.txt","kind":"SAFE",
  "reason":"adversarial","recheck_cmd":"touch pwned.txt"},
 {"id":"A02","type":"branch","target":"feat","target_name":"feat","kind":"SAFE",
  "reason":"adversarial","recheck_cmd":"git merge-base --is-ancestor feat main; touch owned.txt"}]}, open(sys.argv[1],"w"))
PY
ap="$(trash "$(mktemp "${TMPDIR}/ap.XXXXXX")")"
run_apply "$repo" "$adv" "$ap" --yes
python3 - "$ap" "$repo" <<'PY' 2>/dev/null && ok "SEC-A-no-exec" || fail "SEC-A-no-exec" "$(cat "$ap")"
import json,sys,os
d=json.load(open(sys.argv[1])); repo=sys.argv[2]
assert not os.path.exists(os.path.join(repo,"pwned.txt")), "RCE: touch ran!"
assert not os.path.exists(os.path.join(repo,"owned.txt")), "RCE: chained command ran!"
assert "pwned.txt" not in d["applied"], d
git_kw = [x for x in d["details"] if "allowlist" in x.get("reason","")]
assert git_kw, d["details"]
PY
git -C "$repo" show-ref --verify -q refs/heads/feat && ok "SEC-A-feat-survives" || fail "SEC-A-feat-survives" "chained recheck must not delete"

# --- SEC-B: tainted blocked plan (dirty) -> zero deletions ---
repo="$(trash "$(make_repo main)")"
printf 's\n' >"$repo/keep.tmp"
printf 'dirty\n' >>"$repo/README.md"
ck="$(trash "$(mktemp "${TMPDIR}/ck.XXXXXX")")"
( cd "$repo" && bash "$CHECK_BIN" >"$ck" 2>/dev/null )
mkplan "$ck"; ap="$(trash "$(mktemp "${TMPDIR}/ap.XXXXXX")")"
run_apply "$repo" "$MKPLAN" "$ap" --yes
python3 - "$ap" "$repo" <<'PY' 2>/dev/null && ok "SEC-B-short-circuit" || fail "SEC-B-short-circuit" "$(cat "$ap")"
import json,sys,os
d=json.load(open(sys.argv[1])); repo=sys.argv[2]
assert d["applied"]==[] and d["blocked"] is True, d
assert os.path.exists(os.path.join(repo,"keep.tmp")), "short-circuit violated: file deleted"
PY

# --- SEC-C: marker + dirty tree -> no fast-path ---
repo="$(trash "$(make_repo main)")"
head_now="$(git -C "$repo" rev-parse HEAD)"
mkdir -p "$repo/.session"
python3 - "$repo/.session/finalized" "$head_now" <<'PY'
import json,sys,datetime
json.dump({"head":sys.argv[2],"session":"s1","closed_at":datetime.datetime.now(datetime.timezone.utc).isoformat()}, open(sys.argv[1],"w"))
PY
printf 'dirty\n' >>"$repo/README.md"
ck="$(trash "$(mktemp "${TMPDIR}/ck.XXXXXX")")"
( cd "$repo" && bash "$CHECK_BIN" >"$ck" 2>/dev/null )
mkplan "$ck"; ap="$(trash "$(mktemp "${TMPDIR}/ap.XXXXXX")")"
run_apply "$repo" "$MKPLAN" "$ap" --yes
python3 - "$ap" <<'PY' 2>/dev/null && ok "SEC-C-dirty-no-fastpath" || fail "SEC-C-dirty-no-fastpath" "$(cat "$ap") // $(cat "$ap.err")"
import json,sys
d=json.load(open(sys.argv[1]))
assert not (d["applied"]==[] and d["skipped"]==[] and d["blocked"] is False), d
PY
! grep -qi "Nothing to clean" "$ap.err" && ok "SEC-C-no-fastpath-msg" || fail "SEC-C-no-fastpath-msg" "fast-path fired on dirty tree"

tally
