#!/usr/bin/env bash
#
# comment-lint.sh — the comment contract, not grep for its own sake.
#
# Rule (§10c): comments hold only current state plus why-this-way with a
# measurement/card link. Progress retelling stays banned: what the code did
# before, what was done to it.
#
# The naive "now" pattern trap: six live places write "before X, now Y" as the
# choice reason — that is "why", not progress, and grepping "now/before" would
# false-fire on its own norms.
# So "now/before" are NO patterns here: only reading tells them apart, caught by
# the critic as review part, not the script.
#
# The script and its test stay excluded from the check: they must quote the
# patterns in fixtures, or the contract neither checks nor reads.
# The exception runs by file name, two files, no mask: a wide exception would
# turn the gate into decor.
#
# Run: bash scripts/comment-lint.sh [--staged]
#   with no flag — tracked .py/.sh; --staged means staged only.

set -euo pipefail
export LC_ALL=C.UTF-8
ROOT="${COMMENT_LINT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)}"
cd "$ROOT"
if [ "${1:-}" = "--staged" ]; then
  files="$(git diff --cached --name-only --diff-filter=ACM -- '*.py' '*.sh' 2>/dev/null || true)"
else
  files="$(git ls-files -- '*.py' '*.sh' || true)"
fi
# Locale pins to C.UTF-8 inside: uu-grep under C never folds Cyrillic into -i,
# while a bare stem catches all forms with no [[:<:]] (uu-grep never sees a
# Cyrillic word edge). A quote in the [^"'] class breaks -i for Cyrillic too —
# hence two passes: lines with `#` minus lines where the quote precedes `#`,
# then a plain grep -i -F substring.
comment_only() { grep -n '#' "$1" 2>/dev/null | grep -v '"[^#]*#' || true; }
fixed_pat() {
  printf '%s\n' 'добавлен' 'исправлен' 'пофикшен' 'was added' 'was fixed' \
    'was renamed' 'was moved' 'now uses'
}

findings=0
while IFS= read -r file; do
  [ -n "$file" ] || continue
  [ -f "$file" ] || continue
  case "$file" in
    scripts/comment-lint.sh|scripts/comment-lint_test.sh) continue ;;
  esac
  while IFS= read -r hit; do
    findings=$((findings + 1))
    printf 'COMMENT_FINDING %s:%s\n' "$file" "$hit"
  done < <(comment_only "$file" | grep -i -F -f <(fixed_pat) || true)
done <<< "$files"

if [ "$findings" -gt 0 ]; then
  printf 'comment-lint: %s finding(s) — a comment about the code past, rewrite as why\n' "$findings" >&2
  exit 1
fi
printf 'comment-lint: clean\n'
