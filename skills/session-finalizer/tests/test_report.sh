#!/usr/bin/env bash
# test_report.sh — finalize-report <check> <apply> [plan] (stdout=text).
# SESSION FINALIZED <=> apply.blocked==false AND live tree clean.
# Sections + STATUS; BLOCKED extras; exit 1 iff Cleanup/Final state missing.
set -uo pipefail
TESTDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=lib.sh
source "$TESTDIR/lib.sh"
hermetic_git_env; init_trash; trap trash_cleanup EXIT

bash "$REPORT_BIN" --help >/dev/null 2>&1
[ "$?" -eq 0 ] && ok "report-help" || fail "report-help" "exit $?"

# --- FINALIZED: clean tree + unblocked apply ---
repo="$(trash "$(make_repo main)")"
ck="$(trash "$(mktemp "${TMPDIR}/ck.XXXXXX")")"
ap="$(trash "$(mktemp "${TMPDIR}/ap.XXXXXX")")"
( cd "$repo" && bash "$CHECK_BIN" >"$ck" 2>/dev/null )
python3 - "$ap" <<'PY'
import json,sys
json.dump({"applied":["scratch.tmp"],"skipped":[],"blocked":False,"block_reasons":[],"details":[{"target":"scratch.tmp","outcome":"applied","reason":"temp","evidence":"unlinked"}]}, open(sys.argv[1],"w"))
PY
out="$(trash "$(mktemp "${TMPDIR}/r.XXXXXX")")"
run_report "$repo" "$ck" "$ap" "" "$out"
head -1 "$out" | grep -q "SESSION FINALIZED" && [ "$CAP_EXIT" -eq 0 ] && ok "finalized" || fail "finalized" "$(head -3 "$out") exit=$CAP_EXIT"
for s in Task Implementation Git "Issues" Cleanup "Final state" Agents STATUS; do
  grep -q "$s" "$out" || fail "section-$s" "missing section $s"
done
ok "sections-present"

# --- BLOCKED: dirty tree ---
printf 'dirty\n' >>"$repo/README.md"
ck2="$(trash "$(mktemp "${TMPDIR}/ck.XXXXXX")")"
ap2="$(trash "$(mktemp "${TMPDIR}/ap.XXXXXX")")"
( cd "$repo" && bash "$CHECK_BIN" >"$ck2" 2>/dev/null )
python3 - "$ap2" <<'PY'
import json,sys
json.dump({"applied":[],"skipped":["feature"],"blocked":True,"block_reasons":["tree-dirty"],"details":[{"target":"feature","outcome":"skipped","reason":"tree-dirty","evidence":"porcelain non-empty"}]}, open(sys.argv[1],"w"))
PY
out2="$(trash "$(mktemp "${TMPDIR}/r.XXXXXX")")"
run_report "$repo" "$ck2" "$ap2" "" "$out2"
head -1 "$out2" | grep -q "SESSION BLOCKED" && ok "blocked" || fail "blocked" "$(head -3 "$out2")"
for s in Reason "Cleanup performed" "Cleanup NOT performed" "Action required"; do
  grep -q "$s" "$out2" || fail "bsection-$s" "missing $s"
done
ok "blocked-sections"
# marks present
grep -Eq '✓|!|\?' "$out2" && ok "marks" || fail "marks" "no marks"

tally
