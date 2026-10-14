#!/usr/bin/env bash
#
# comment-lint_test.sh — the comment-contract checks.
#
# The gate stands on the path to every commit, testing strict:
# not "the script launches" but "every refusal fires as intended".
# A silent gate reads worse than a missing one.
#
# The test runs the script itself (COMMENT_LINT_ROOT on an isolated git root),
# never a duplicate of its pattern: the duplicate drifts from the original on the
# very first edit, testing no longer the gate. Every case runs under C and under
# UTF-8 — Cyrillic must catch in both (the script pins LC_ALL itself, proven by
# the test, never assumed).
#
# Run: bash scripts/comment-lint_test.sh

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
LINT="$ROOT/scripts/comment-lint.sh"
TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/comment-lint-tests.XXXXXX")"
trap 'rm -rf "$TMP_ROOT"' EXIT

passed=0

fail() {
  printf 'FAIL: %s\n' "$*" >&2
  exit 1
}

# An isolated git root with one f.py file.
fresh_root() {
  rm -rf "$TMP_ROOT/case"
  mkdir -p "$TMP_ROOT/case"
  (cd "$TMP_ROOT/case" && git init -q && git config user.email t@t && git config user.name t)
  printf '%s\nx = 1\n' "$1" > "$TMP_ROOT/case/f.py"
  (cd "$TMP_ROOT/case" && git add f.py)
}

# Slop catches: the script reddens under both locales.
check_slop() {
  for loc in C C.utf8; do
    fresh_root "$1"
    if COMMENT_LINT_ROOT="$TMP_ROOT/case" LC_ALL="$loc" bash "$LINT" >/dev/null 2>&1; then
      fail "slop missed [$loc]: $1"
    else
      passed=$((passed + 1))
    fi
  done
}

# Norm passes: the script stays green under both locales.
check_norm() {
  for loc in C C.utf8; do
    fresh_root "$1"
    if COMMENT_LINT_ROOT="$TMP_ROOT/case" LC_ALL="$loc" bash "$LINT" >/dev/null 2>&1; then
      passed=$((passed + 1))
    else
      fail "norm caught as slop [$loc]: $1"
    fi
  done
}

check_slop "# Исправлено падение при пустом списке"
check_slop "# добавлена проверка на закрытый раунд"
check_slop "# was fixed by retrying the pane lookup"
check_slop "# was added to handle empty state"
check_slop "# now uses the session address"
check_slop "# was renamed for clarity"

check_norm "# Раньше печать была первым шагом, теперь это исключение"
check_norm "# Тот же обман, что в session_address, только теперь он бьет по другой линии"
check_norm "# empty means an explicit ban, not a missing file"

# The live tree stays clean under both locales.
for loc in C C.utf8; do
  if LC_ALL="$loc" bash "$LINT" >/dev/null 2>&1; then
    passed=$((passed + 1))
  else
    fail "the live tree fails its own lint [$loc]"
  fi
done

# The script and its test exclude by name — fixtures must quote the patterns.
if grep -q "comment-lint.sh|scripts/comment-lint_test.sh" "$LINT"; then
  passed=$((passed + 1))
else
  fail "the script misses the self-and-test exclusion"
fi

printf 'comment-lint tests: %s passed\n' "$passed"
