#!/usr/bin/env bash
#
# comment-lint_test.sh — проверки договора о комментариях.
#
# Гейт стоит на пути к каждому коммиту, поэтому тестируется строго:
# не «скрипт запускается», а «каждый его отказ срабатывает по назначению».
# Молчаливый гейт хуже отсутствующего.
#
# Тест гоняет сам скрипт (COMMENT_LINT_ROOT на изолированный git-корень),
# а не дубликат его паттерна: дубликат расходится с оригиналом при первой
# же правке, и тест проверяет уже не гейт. Каждый кейс идёт под C и под
# UTF-8 — кириллица обязана ловиться в обеих (скрипт пинит LC_ALL сам,
# тест это доказывает, а не предполагает).
#
# Запуск: bash scripts/comment-lint_test.sh

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

# Изолированный git-корень с одним файлом f.py.
fresh_root() {
  rm -rf "$TMP_ROOT/case"
  mkdir -p "$TMP_ROOT/case"
  (cd "$TMP_ROOT/case" && git init -q && git config user.email t@t && git config user.name t)
  printf '%s\nx = 1\n' "$1" > "$TMP_ROOT/case/f.py"
  (cd "$TMP_ROOT/case" && git add f.py)
}

# Слоп ловится: скрипт краснеет под обеими локалями.
check_slop() {
  for loc in C C.utf8; do
    fresh_root "$1"
    if COMMENT_LINT_ROOT="$TMP_ROOT/case" LC_ALL="$loc" bash "$LINT" >/dev/null 2>&1; then
      fail "слоп не пойман [$loc]: $1"
    else
      passed=$((passed + 1))
    fi
  done
}

# Норма проходит: скрипт зелёный под обеими локалями.
check_norm() {
  for loc in C C.utf8; do
    fresh_root "$1"
    if COMMENT_LINT_ROOT="$TMP_ROOT/case" LC_ALL="$loc" bash "$LINT" >/dev/null 2>&1; then
      passed=$((passed + 1))
    else
      fail "норма поймана как слоп [$loc]: $1"
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

# Живое дерево чисто под обеими локалями.
for loc in C C.utf8; do
  if LC_ALL="$loc" bash "$LINT" >/dev/null 2>&1; then
    passed=$((passed + 1))
  else
    fail "живое дерево не проходит собственный lint [$loc]"
  fi
done

# Сам скрипт и его тест исключены по имени — фикстуры обязаны цитировать паттерны.
if grep -q "comment-lint.sh|scripts/comment-lint_test.sh" "$LINT"; then
  passed=$((passed + 1))
else
  fail "в скрипте нет исключения для себя и своего теста"
fi

printf 'comment-lint tests: %s passed\n' "$passed"
