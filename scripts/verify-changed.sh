#!/usr/bin/env bash
# verify-changed: static plus only the touched suites (5.5 min down to seconds).
# Usage: ./scripts/verify-changed.sh [--base REF] [--all]
#   --base REF  diff against REF (default — uncommitted + HEAD)
#   --all       show the selection with no launch
#   --suite-for PATH  print the suite this path would select, and nothing else
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
ROOT="$(dirname "$SCRIPT_DIR")"
MAP="$SCRIPT_DIR/verify-map.tsv"
BASE=""
DRY=0
PROBE=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --base) BASE="${2:-}"; shift 2 || { echo "error: --base needs REF" >&2; exit 2; } ;;
    --all|--dry-run) DRY=1; shift ;;
    --suite-for) PROBE="${2:-}"; shift 2 || { echo "error: --suite-for needs PATH" >&2; exit 2; } ;;
    *) echo "usage: $0 [--base REF] [--all] [--suite-for PATH]" >&2; exit 2 ;;
  esac
done
cd "$ROOT"

# The suite a changed path selects. Kept in one place so the probe below and
# the loop further down cannot disagree.
suite_for() {
  local f="$1" suite exact follow
  # The map's rows are either an exact path or a DIRECTORY PREFIX written with
  # its trailing slash ("skills/", "scripts/", "docs/"). Two things were wrong
  # here and both cost the gate its coverage:
  #
  #   `$f` in awk is FIELD f, not the variable f - so the comparison was
  #   between the whole record and the first field, and the directory rows
  #   ("skills/") never matched a file inside them. The rule is `f == $1`.
  #   `index($f, $1"/")` appends a slash to a prefix that already ends in one
  #   ("skills//"), so even the corrected comparison found nothing. The rule is
  #   `index(f, $1) == 1`.
  #
  # The symptom was silent: a change under skills/ selected NO suite at all,
  # so the changed-file gate ran static and nothing else - and the map's own
  # directory rows read as if they were covered.
  suite=$(awk -F'\t' -v f="$f" 'f == $1 || index(f, $1) == 1 {print $2; found=1} END {if (!found) print "UNKNOWN"}' "$MAP" | head -1)
  # An exact match beats the prefix: the awk above gives the first match — a
  # scripts/x.sh file matches both scripts/ and its own line; the exact one we find.
  exact=$(awk -F'\t' -v f="$f" '$1 == f {print $2}' "$MAP" | head -1)
  [ -n "$exact" ] && suite="$exact"
  # Cross-suite pins: test_protocol asserts output of modules mapped elsewhere
  # (STOP seq 1187: 7 failures hid in green slices). follow:<file> rows in the
  # map add the integration suite on top of the file's own.
  follow=$(awk -F'\t' -v f="follow:$f" '$1 == f {print $2}' "$MAP" | head -1)
  [ -n "$follow" ] && suite="$suite;$follow"
  printf '%s\n' "$suite"
}

if [ -n "$PROBE" ]; then
  suite_for "$PROBE"
  exit 0
fi
if [ -n "$BASE" ]; then
  files=$(git diff --name-only "$BASE" -- . ':!node_modules')
else
  files=$( (git diff --name-only -- . ':!node_modules'; git ls-files --others --exclude-standard) | sort -u)
fi
[ -n "$files" ] || { echo "verify-changed: nothing to check, the tree is clean"; exit 0; }
suites=""
while IFS= read -r f; do
  [ -n "$f" ] || continue
  selected=$(suite_for "$f")
  # One path can select two suites (its own plus a follow: pin); both run.
  while IFS= read -r s; do
    [ -n "$s" ] || continue
    case ";$suites;" in *";$s;"*) ;; *) suites="$suites;$s" ;; esac
  done <<< "$(printf '%s\n' "$selected" | tr ';' '\n')"
done <<< "$files"
suites="${suites#;}"
echo "verify-changed: files: $(echo "$files" | tr '\n' ' ')"
echo "verify-changed: suites: $suites"
[ "$DRY" -eq 1 ] && exit 0
# static always: file-size-gate and comment-lint catch what no suite sees.
"$SCRIPT_DIR/verify-project.sh" --only static || exit $?
fail=0
for s in $(echo "$suites" | tr ';' ' '); do
  case "$s" in
    UNKNOWN) echo "verify-changed: UNKNOWN — no mapping, running the whole positive" >&2
      "$SCRIPT_DIR/verify-project.sh" --only positive || fail=1; break ;;
    installer-subset) echo "verify-changed: [installer-subset] PAIR_INSTALL_FAST=1 python3 install_test.py"
      PAIR_INSTALL_FAST=1 python3 install_test.py || { echo "verify-changed: installer-subset FAILED" >&2; fail=1; } ;;
    *-check|docs-check) echo "verify-changed: $s — no separate suite, covered by static" ;;
    *) cmd=$(awk -F'\t' -v s="$s" '$1 == s {print $4; exit}' "$SCRIPT_DIR/verification-profile.tsv")
      if [ -z "$cmd" ]; then echo "verify-changed: suite $s missing from the profile" >&2; fail=1; continue; fi
      echo "verify-changed: [$s] $cmd"
      bash -c "$cmd" || { echo "verify-changed: $s FAILED" >&2; fail=1; } ;;
  esac
done
exit $fail
