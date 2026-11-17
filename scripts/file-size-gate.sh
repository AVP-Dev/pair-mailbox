#!/usr/bin/env bash
#
# file-size-gate.sh — advisory size guidance that surfaces review boundaries.
#
# Ported in spirit from the agent-starter-kit rule in
# docs/project-structure.md §2 of that project: a source file is the normal
# target at up to 300 lines, 300-500 is acceptable while the file keeps a
# single responsibility, 500-800 suggests reviewing its boundaries, and
# anything above 800 deserves an explicit review and a split plan.
#
# Why a ratchet instead of a split today: the same kit forbids performing a
# mass refactor alongside an unrelated task (docs/project-structure.md §9, and
# AGENTS.md §4.7 requires ownership, scope and the reason for a large file to
# be fixed in a design manifest first). So mcp/pair_mailbox/server.py at 3284
# lines is not rewritten today. Its current size is recorded as context for
# reviewers; crossing that number must never stop otherwise safe work.
#
# A baseline entry is a review note about a large file and its reason. It is
# advisory, not an exception an agent must obtain before continuing work.
#
# Malformed baseline data is an error; stale paths and size changes are reported
# as advisories so maintenance guidance cannot become an execution tripwire.
#
# Scope: source code only — .py, .sh, .ts, .js, .mjs, .cjs, plus extensionless
# executable scripts such as bin/pairpilot. Markdown documentation is
# deliberately out of scope, and that is stated here on purpose rather than
# left implicit: prose has no single responsibility to protect, and ratcheting
# it would only manufacture busywork.
#
# Usage: ./scripts/file-size-gate.sh [--baseline PATH]
# Exit codes: 0 completed (possibly with size advisories), 2 scanner/baseline error.

set -euo pipefail

# Associative arrays below need bash 4+; macOS ships 3.2 (round 92 C8).
if [ -z "${BASH_VERSINFO:-}" ] || [ "${BASH_VERSINFO[0]}" -lt 4 ]; then
  echo "file-size-gate: need bash 4 or newer (macOS: brew install bash)" >&2
  exit 2
fi

usage() {
  printf '%s\n' "Usage: $0 [--baseline PATH]" \
    "  --baseline PATH  baseline relative to the project root (default scripts/file-size-baseline.tsv)"
}

fail() {
  printf 'file-size-gate: %s\n' "$*" >&2
  exit 2
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
BASELINE="scripts/file-size-baseline.tsv"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --baseline)
      [ "$#" -ge 2 ] || fail "--baseline needs a path"
      BASELINE="$2"
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      printf 'Unknown option: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

case "$BASELINE" in
  /*) BASELINE_FILE="$BASELINE" ;;
  *) BASELINE_FILE="$PROJECT_ROOT/$BASELINE" ;;
esac

TARGET_MAX=300
REVIEW_MAX=800

cd "$PROJECT_ROOT"

if [ ! -f "$BASELINE_FILE" ]; then
  fail "baseline not found: $BASELINE_FILE"
fi
if [ ! -r "$BASELINE_FILE" ]; then
  fail "baseline not readable: $BASELINE_FILE"
fi

# A file counts as source if it has a measured extension, or if it has no
# extension at all and carries the executable bit (bin/pairpilot, bin/pairctl).
is_source_file() {
  local path="$1" name
  name="${path##*/}"
  case "$name" in
    *.py|*.sh|*.ts|*.js|*.mjs|*.cjs)
      return 0
      ;;
  esac
  case "$name" in
    *.*)
      return 1
      ;;
  esac
  [ -f "$path" ] && [ -x "$path" ]
}

line_count() {
  local path="$1"
  if [ ! -r "$path" ]; then
    fail "file not readable: $path"
  fi
  wc -l <"$path" | tr -d '[:space:]'
}

# Collect candidates: tracked files plus untracked-but-not-ignored ones, so a
# brand new oversized file cannot slip through by simply not being added yet.
if command -v git >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  candidates="$(git ls-files --cached --others --exclude-standard)"
else
  candidates="$(find . \( -type d \( -name .git -o -name node_modules -o -name vendor -o -name .venv -o -name venv -o -name dist -o -name build -o -name __pycache__ \) -prune \) -o -type f -print | sed 's|^\./||')"
fi

[ -n "$candidates" ] || fail "no files found to check"

# --- baseline: parse, validate, index ---------------------------------------

declare -A allowed_max=()
declare -A allowed_reason=()
line_number=0
baseline_entries=0

while IFS=$'\t' read -r path max_lines reason extra || [ -n "${path:-}${max_lines:-}${reason:-}${extra:-}" ]; do
  line_number=$((line_number + 1))
  path="${path%$'\r'}"
  max_lines="${max_lines%$'\r'}"
  reason="${reason%$'\r'}"
  extra="${extra%$'\r'}"

  case "$path" in
    ''|'#'*) continue ;;
    \#*) continue ;;
  esac
  # A bare header line counts as the header. Or an author copying the format from the
  # example, forgetting `#`, takes a confusing
  # "max_lines must be an integer, got 'max_lines'" instead of at once seeing the
  # line is no data.
  if [ "$path" = "path" ] && [ "$max_lines" = "max_lines" ] && [ "$reason" = "reason" ]; then
    continue
  fi

  [ -z "$extra" ] || fail "line $line_number: extra fields past reason"
  [ -n "$reason" ] || fail "line $line_number: reason is mandatory, a reasonless exception never counts as pinned"
  [[ "$max_lines" =~ ^[0-9]+$ ]] || fail "line $line_number: max_lines must be an integer, got '$max_lines'"
  [ "$max_lines" -gt 0 ] || fail "line $line_number: max_lines must be positive"
  case ";${allowed_max_keys:-};" in
    *";$path;"*) fail "line $line_number: duplicate path '$path'" ;;
  esac
  allowed_max_keys="${allowed_max_keys:-};$path"
  allowed_max["$path"]="$max_lines"
  allowed_reason["$path"]="$reason"
  baseline_entries=$((baseline_entries + 1))
done <"$BASELINE_FILE"

[ "$baseline_entries" -gt 0 ] || fail "baseline holds no records"

# --- measure every source file ----------------------------------------------

advisories=0
reviews=0
noted=0
clean=0
declare -a report=()

while IFS= read -r file; do
  [ -n "$file" ] || continue
  [ -L "$file" ] && continue
  is_source_file "$file" || continue

  lines="$(line_count "$file")"
  cap="${allowed_max["$file"]:-}"

  if [ -n "$cap" ]; then
    if [ "$lines" -gt "$cap" ]; then
      advisories=$((advisories + 1))
      report+=("ADVISORY	$file	$lines lines, review note was $cap (${allowed_reason["$file"]})")
    else
      noted=$((noted + 1))
    fi
    if [ "$lines" -gt "$REVIEW_MAX" ]; then
      advisories=$((advisories + 1))
      report+=("ADVISORY	$file	$lines lines, above review threshold $REVIEW_MAX; inspect boundaries when useful")
    fi
    continue
  fi

  if [ "$lines" -gt "$REVIEW_MAX" ]; then
    advisories=$((advisories + 1))
    report+=("ADVISORY	$file	$lines lines, above review threshold $REVIEW_MAX; inspect boundaries when useful")
  elif [ "$lines" -gt "$TARGET_MAX" ]; then
    reviews=$((reviews + 1))
    report+=("REVIEW	$file	$lines lines, target size $TARGET_MAX: bounds need review")
  else
    clean=$((clean + 1))
  fi
done <<<"$candidates"

# --- baseline entries that no longer describe reality ------------------------

while IFS= read -r file; do
  [ -n "$file" ] || continue
  cap="${allowed_max["$file"]:-}"
  [ -n "$cap" ] || continue
  if [ ! -f "$file" ]; then
    advisories=$((advisories + 1))
    report+=("ADVISORY	$file	baseline path is stale; remove or update the review note when convenient")
    continue
  fi
  lines="$(line_count "$file")"
  # Size review notes go stale when the file no longer crosses the review band.
  # Report that fact, but do not require a bookkeeping edit to continue work.
  if [ "$lines" -le "$REVIEW_MAX" ] && [ "$cap" -gt "$REVIEW_MAX" ]; then
    advisories=$((advisories + 1))
    report+=("ADVISORY	$file	$lines lines, below review threshold $REVIEW_MAX; old note may be removed")
  fi
done <<<"$(printf '%s\n' "${!allowed_max[@]}" | sort)"

# --- report ------------------------------------------------------------------

for entry in "${report[@]+"${report[@]}"}"; do
  printf '%s\n' "$entry"
done

printf 'file-size-gate: complete (%s advisory note(s), %s files for review, %s noted, %s fine; review threshold %s)\n' \
  "$advisories" "$reviews" "$noted" "$clean" "$REVIEW_MAX"
