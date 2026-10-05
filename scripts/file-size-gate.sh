#!/usr/bin/env bash
#
# file-size-gate.sh — a ratchet that keeps source files from growing past a
# size someone has actually reviewed.
#
# Ported in spirit from the agent-starter-kit rule in
# docs/project-structure.md §2 of that project: a source file is the normal
# target at up to 300 lines, 300-500 is acceptable while the file keeps a
# single responsibility, 500-800 forces a review of its boundaries, and
# anything above 800 is allowed only with a recorded reason and a split plan.
#
# Why a ratchet instead of a split today: the same kit forbids performing a
# mass refactor alongside an unrelated task (docs/project-structure.md §9, and
# AGENTS.md §4.7 requires ownership, scope and the reason for a large file to
# be fixed in a design manifest first). So mcp/pair_mailbox/server.py at 3284
# lines is not rewritten today. It is frozen at its current size instead: it
# may shrink, and shrinking is encouraged, but it may not grow by one line
# unless someone records a new and larger exception with a reason.
#
# A baseline entry means: reviewed, and frozen at that CEILING, for a stated
# reason. The reason is mandatory, because an exception nobody can explain is
# just a number that hides a problem. The cap is a ceiling, not an exact size:
# the file may sit far below it for as long as it likes, and that is fine.
#
# Entries that point at a missing file are errors, and so is an entry whose file
# has dropped back under the hard threshold: the exception no longer describes
# anything. A ratchet that never tightens silently stops describing reality.
#
# Scope: source code only — .py, .sh, .ts, .js, .mjs, .cjs, plus extensionless
# executable scripts such as bin/pairpilot. Markdown documentation is
# deliberately out of scope, and that is stated here on purpose rather than
# left implicit: prose has no single responsibility to protect, and ratcheting
# it would only manufacture busywork.
#
# Usage: ./scripts/file-size-gate.sh [--baseline PATH]
# Exit codes: 0 clean, 1 size violation, 2 scanner or baseline error.

set -euo pipefail

usage() {
  printf '%s\n' "Использование: $0 [--baseline PATH]" \
    "  --baseline PATH  baseline относительно корня проекта (по умолчанию scripts/file-size-baseline.tsv)"
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
      [ "$#" -ge 2 ] || fail "--baseline требует путь"
      BASELINE="$2"
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      printf 'Неизвестная опция: %s\n' "$1" >&2
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
HARD_MAX=800

cd "$PROJECT_ROOT"

if [ ! -f "$BASELINE_FILE" ]; then
  fail "baseline не найден: $BASELINE_FILE"
fi
if [ ! -r "$BASELINE_FILE" ]; then
  fail "baseline недоступен для чтения: $BASELINE_FILE"
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
    fail "файл недоступен для чтения: $path"
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

[ -n "$candidates" ] || fail "не найдено ни одного файла для проверки"

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
  # Незакомментированная строка заголовка принимается как заголовок. Иначе автор,
  # скопировавший формат из примера и забывший `#`, получает невнятное
  # «max_lines должен быть целым числом, получено 'max_lines'» вместо того,
  # чтобы сразу увидеть, что строка не данные.
  if [ "$path" = "path" ] && [ "$max_lines" = "max_lines" ] && [ "$reason" = "reason" ]; then
    continue
  fi

  [ -z "$extra" ] || fail "строка $line_number: лишние поля после reason"
  [ -n "$reason" ] || fail "строка $line_number: reason обязателен, исключение без причины не считается зафиксированным"
  [[ "$max_lines" =~ ^[0-9]+$ ]] || fail "строка $line_number: max_lines должен быть целым числом, получено '$max_lines'"
  [ "$max_lines" -gt 0 ] || fail "строка $line_number: max_lines должен быть положительным"
  case ";${allowed_max_keys:-};" in
    *";$path;"*) fail "строка $line_number: повторяющийся путь '$path'" ;;
  esac
  allowed_max_keys="${allowed_max_keys:-};$path"
  allowed_max["$path"]="$max_lines"
  allowed_reason["$path"]="$reason"
  baseline_entries=$((baseline_entries + 1))
done <"$BASELINE_FILE"

[ "$baseline_entries" -gt 0 ] || fail "baseline не содержит ни одной записи"

# --- measure every source file ----------------------------------------------

violations=0
reviews=0
frozen=0
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
      violations=$((violations + 1))
      report+=("VIOLATION	$file	$lines строк, заморожено на $cap: рост запрещён (${allowed_reason["$file"]})")
    else
      frozen=$((frozen + 1))
    fi
    continue
  fi

  if [ "$lines" -gt "$HARD_MAX" ]; then
    violations=$((violations + 1))
    report+=("VIOLATION	$file	$lines строк, жёсткий порог $HARD_MAX: нужен зафиксированный exception в $BASELINE")
  elif [ "$lines" -gt "$TARGET_MAX" ]; then
    reviews=$((reviews + 1))
    report+=("REVIEW	$file	$lines строк, целевой размер $TARGET_MAX: границы подлежат ревью")
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
    violations=$((violations + 1))
    report+=("VIOLATION	$file	baseline ссылается на отсутствующий файл, запись протухла")
    continue
  fi
  lines="$(line_count "$file")"
  # Исключение протухает только тогда, когда файл ПЕРЕСТАЛ нуждаться в нём.
  # Cap — это потолок, а не точное значение, поэтому требовать его опустить при
  # любом изменении файла нельзя: тогда любая правка server.py требовала бы
  # правки baseline, и гейт стал бы обязательным препятствием вместо рэтчета.
  # Протухание — это ровно одно состояние: файл ушёл под жёсткий порог, а запись
  # по-прежнему его оправдывает.
  if [ "$lines" -le "$HARD_MAX" ] && [ "$cap" -gt "$HARD_MAX" ]; then
    violations=$((violations + 1))
    report+=("VIOLATION	$file	$lines строк, уже не превышает жёсткий порог $HARD_MAX: запись протухла, опусти cap до $lines или удали строку")
  fi
done <<<"$(printf '%s\n' "${!allowed_max[@]}" | sort)"

# --- report ------------------------------------------------------------------

for entry in "${report[@]+"${report[@]}"}"; do
  printf '%s\n' "$entry"
done

if [ "$violations" -gt 0 ]; then
  printf 'file-size-gate: %s violation(s), %s file(s) на ревью, %s заморожено, %s в норме\n' \
    "$violations" "$reviews" "$frozen" "$clean" >&2
  exit 1
fi

printf 'file-size-gate: clean (%s заморожено, %s на ревью, %s в норме, порог %s)\n' \
  "$frozen" "$reviews" "$clean" "$HARD_MAX"