#!/usr/bin/env bash
# run.sh — run every tests/test_*.sh in a fresh process; pass/fail + count.
# Nonzero exit on any failure; empty run (0 tests) is red.
# Guards the real umputum worktree: git status must be identical before/after.
set -euo pipefail
TESTDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
cd "$TESTDIR"
for f in test_*.sh lib.sh run.sh; do
  [ -f "$f" ] && bash -n "$f" || { printf 'SYNTAX FAIL: %s\n' "$f" >&2; exit 1; }
done
REAL_TOP="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
REAL_BEFORE="$(git -C "$REAL_TOP" status --porcelain 2>/dev/null || true)"
REAL_HEAD_BEFORE="$(git -C "$REAL_TOP" rev-parse HEAD 2>/dev/null || true)"
files_pass=0; files_fail=0; cases_pass=0; cases_fail=0; failed_files=""; nfiles=0
for t in test_*.sh; do
  nfiles=$((nfiles + 1))
  printf '=== %s ===\n' "$t"
  log="$(mktemp "${TMPDIR:-/tmp}/finalize-run.XXXXXX")"
  set +e
  bash "$t" 2>&1 | tee "$log"
  code="${PIPESTATUS[0]}"
  set -e
  p="$(grep -c '^PASS ' "$log" || true)"
  f="$(grep -c '^FAIL ' "$log" || true)"
  cases_pass=$((cases_pass + p)); cases_fail=$((cases_fail + f))
  if [ "$code" -ne 0 ] || [ "$f" -gt 0 ]; then
    files_fail=$((files_fail + 1)); failed_files="$failed_files $t"
    [ "$f" -eq 0 ] && printf 'FAIL %s: exited %d with no verdict\n' "$t" "$code"
  elif [ "$p" -eq 0 ]; then
    files_fail=$((files_fail + 1)); failed_files="$failed_files $t(empty)"
  else
    files_pass=$((files_pass + 1))
  fi
  rm -f "$log"
done
[ "$nfiles" -eq 0 ] && { printf 'FAIL: empty run (0 test files)\n' >&2; exit 1; }
[ "$cases_pass" -eq 0 ] && { printf 'FAIL: empty run (0 passing cases)\n' >&2; exit 1; }
REAL_AFTER="$(git -C "$REAL_TOP" status --porcelain 2>/dev/null || true)"
REAL_HEAD_AFTER="$(git -C "$REAL_TOP" rev-parse HEAD 2>/dev/null || true)"
guard_ok=1
[ "$REAL_BEFORE" = "$REAL_AFTER" ] || { guard_ok=0; printf 'GUARD FAIL: real repo worktree changed\n' >&2; }
[ "$REAL_HEAD_BEFORE" = "$REAL_HEAD_AFTER" ] || { guard_ok=0; printf 'GUARD FAIL: real repo HEAD moved\n' >&2; }
printf '\n---- suite summary ----\n'
printf 'files: pass=%d fail=%d\n' "$files_pass" "$files_fail"
printf 'cases: pass=%d fail=%d\n' "$cases_pass" "$cases_fail"
[ -n "$failed_files" ] && printf 'failed:%s\n' "$failed_files"
[ "$guard_ok" -eq 1 ] && [ "$files_fail" -eq 0 ] && [ "$cases_fail" -eq 0 ]
