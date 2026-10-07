#!/usr/bin/env bash
#
# comment-lint.sh — договор о комментариях, а не греп ради грепа.
#
# Правило (§10c): в комментариях только текущее состояние + «почему так,
# а не иначе» со ссылкой на измерение/карточку. Пересказ прогресса под
# запретом: что код делал раньше и что с ним сделали.
#
# Ловушка наивного паттерна «теперь»: шесть живых мест пишут «раньше Х,
# теперь Y» как объяснение выбора — это «почему», а не прогресс, и греп
# по «теперь/раньше» дал бы ложные срабатывания на своих же нормах.
# Поэтому «теперь/раньше» здесь НЕ паттерны: их различает только чтение,
# и их ловит критик как часть ревью, а не скрипт.
#
# Сам скрипт и его тест из проверки исключены: они обязаны цитировать
# паттерны в фикстурах, иначе договор нельзя ни проверить, ни прочитать.
# Исключение — по имени файла, два файла, не маска: широкое исключение
# превратило бы гейт в декорацию.
#
# Запуск: bash scripts/comment-lint.sh [--staged]
#   без флага — tracked .py/.sh; --staged — только staged.

set -euo pipefail
export LC_ALL=C.UTF-8
ROOT="${COMMENT_LINT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)}"
cd "$ROOT"
if [ "${1:-}" = "--staged" ]; then
  files="$(git diff --cached --name-only --diff-filter=ACM -- '*.py' '*.sh' 2>/dev/null || true)"
else
  files="$(git ls-files -- '*.py' '*.sh' || true)"
fi
# Локаль прибита к C.UTF-8 внутри: uu-grep под C не складывает кириллицу
# в -i, а корень без окончания ловит все формы без [[:<:]] (границу
# кириллического слова uu-grep не видит). Кавычка в классе [^"'] тоже
# ломает -i для кириллицы — поэтому два прохода: строки с `#`, минус
# строки где кавычка раньше `#`, потом простая подстрока grep -i -F.
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
  printf 'comment-lint: %s finding(s) — коммент про прошлое кода, перепиши на «почему»\n' "$findings" >&2
  exit 1
fi
printf 'comment-lint: clean\n'
