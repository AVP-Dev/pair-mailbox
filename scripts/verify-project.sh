#!/usr/bin/env bash
set -euo pipefail

usage() {
  printf '%s\n' "Использование: $0 [--profile PATH] [--only LAYER[,LAYER...]] [--list] [--dry-run] [--jobs N]" \
    "  --profile PATH  профиль команд относительно корня проекта" \
    "  --only LAYERS   запустить только указанные слои" \
    "  --list          показать профиль без запуска" \
    "  --dry-run       показать выбранные проверки без запуска" \
    "  --jobs N        параллельные проверки (по умолчанию 1; сюиты изолированы: temp-state, уникальные tmux-имена)"
}

fail() {
  printf 'verify-project: %s\n' "$*" >&2
  exit 2
}

PROFILE_PATH="scripts/verification-profile.tsv"
ONLY=""
LIST_ONLY=0
DRY_RUN=0
JOBS=1
seen_names=""

while [ "$#" -gt 0 ]; do
  case "$1" in
    --profile)
      [ "$#" -ge 2 ] || fail "--profile требует путь"
      PROFILE_PATH="$2"
      shift 2
      ;;
    --only)
      [ "$#" -ge 2 ] || fail "--only требует список слоёв"
      ONLY="$2"
      shift 2
      ;;
    --list)
      LIST_ONLY=1
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --jobs)
      [ "$#" -ge 2 ] || fail "--jobs требует число"
      [[ "$2" =~ ^[1-9][0-9]*$ ]] || fail "--jobs: нужно положительное число, получили '$2'"
      JOBS="$2"
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

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

case "$PROFILE_PATH" in
  /*) PROFILE_FILE="$PROFILE_PATH" ;;
  *) PROFILE_FILE="$PROJECT_ROOT/$PROFILE_PATH" ;;
esac

valid_layer() {
  case " static positive negative security regression integration build " in
    *" $1 "*) return 0 ;;
    *) return 1 ;;
  esac
}

selected_layer() {
  [ -z "$ONLY" ] && return 0
  case ",$ONLY," in
    *",$1,"*) return 0 ;;
    *) return 1 ;;
  esac
}

if [ -n "$ONLY" ]; then
  case "$ONLY" in
    ,*|*,|*,,*) fail "пустой слой в --only" ;;
  esac
  IFS=',' read -r -a requested_layers <<<"$ONLY"
  [ "${#requested_layers[@]}" -gt 0 ] || fail "пустой --only"
  for layer in "${requested_layers[@]}"; do
    valid_layer "$layer" || fail "неизвестный слой: $layer"
  done
fi

[ -f "$PROFILE_FILE" ] || fail "профиль не найден: $PROFILE_FILE"
[ -r "$PROFILE_FILE" ] || fail "профиль недоступен для чтения: $PROFILE_FILE"

cd "$PROJECT_ROOT"

found=0
selected=0
failures=0
warnings=0
line_number=0
running=0
rm -f "$PROJECT_ROOT/.pair/verify-results.txt" "$PROJECT_ROOT/.pair/verify-"*.log

while IFS=$'\t' read -r name layer required command extra || [ -n "${name:-}${layer:-}${required:-}${command:-}${extra:-}" ]; do
  line_number=$((line_number + 1))
  name="${name%$'\r'}"
  layer="${layer%$'\r'}"
  required="${required%$'\r'}"
  command="${command%$'\r'}"
  extra="${extra%$'\r'}"

  [ -n "$name" ] || continue
  case "$name" in
    \#*) continue ;;
  esac

  [ -z "$extra" ] || fail "строка $line_number: лишние поля"
  [[ "$name" =~ ^[A-Za-z0-9._-]+$ ]] || fail "строка $line_number: недопустимое имя проверки"
  valid_layer "$layer" || fail "строка $line_number: неизвестный слой '$layer'"
  case "$required" in
    0|1) ;;
    *) fail "строка $line_number: required должен быть 0 или 1" ;;
  esac
  [ -n "$command" ] || fail "строка $line_number: команда не может быть пустой"
  case ";$seen_names;" in
    *";$name;"*) fail "строка $line_number: повторяющееся имя проверки '$name'" ;;
  esac
  seen_names="$seen_names;$name"
  found=1

  if [ "$LIST_ONLY" -eq 1 ]; then
    if selected_layer "$layer"; then
      printf '%s\t%s\t%s\t%s\n' "$name" "$layer" "$required" "$command"
    else
      printf '%s\t%s\t%s\t%s\tskipped\n' "$name" "$layer" "$required" "$command"
    fi
    continue
  fi

  selected_layer "$layer" || continue
  selected=1

  if [ "$DRY_RUN" -eq 1 ]; then
    printf '[verify] dry-run: %s\n' "$command"
    continue
  fi

  # Параллельность пачками по JOBS: каждая проверка пишет в свой лог,
  # итог собирается после wait. Вывод сюиты не перемежается с чужой.
  printf '[verify] %s (%s)\n' "$name" "$layer"
  log="$PROJECT_ROOT/.pair/verify-$name.log"
  ( bash -c "$command" >"$log" 2>&1; echo "$? $required $name" >>"$PROJECT_ROOT/.pair/verify-results.txt" ) &
  running=$((running + 1))
  if [ "$running" -ge "$JOBS" ]; then
    wait -n
    running=$((running - 1))
  fi
done <"$PROFILE_FILE"

# Добираем хвост пачки и сводим итоги из файла результатов.
wait
if [ -f "$PROJECT_ROOT/.pair/verify-results.txt" ]; then
  while read -r status required name; do
    if [ "$status" -eq 0 ]; then
      printf '[verify] %s: passed\n' "$name"
    elif [ "$required" -eq 1 ]; then
      failures=$((failures + 1))
      printf '[verify] %s: failed (exit %s, required)\n' "$name" "$status" >&2
      printf '[verify] %s: --- tail %s ---\n' "$name" "$PROJECT_ROOT/.pair/verify-$name.log" >&2
      tail -15 "$PROJECT_ROOT/.pair/verify-$name.log" >&2
    else
      warnings=$((warnings + 1))
      printf '[verify] %s: warning (exit %s, optional)\n' "$name" "$status" >&2
      tail -5 "$PROJECT_ROOT/.pair/verify-$name.log" >&2
    fi
  done <"$PROJECT_ROOT/.pair/verify-results.txt"
  rm -f "$PROJECT_ROOT/.pair/verify-results.txt"
fi

[ "$found" -eq 1 ] || fail "профиль не содержит проверок"

if [ "$LIST_ONLY" -eq 1 ]; then
  exit 0
fi

[ "$selected" -eq 1 ] || fail "не выбрано ни одной проверки для запуска"

if [ "$failures" -gt 0 ]; then
  printf '[verify] failed: %s required check(s), %s warning(s)\n' "$failures" "$warnings" >&2
  exit 1
fi

printf '[verify] passed: %s required failure(s), %s warning(s)\n' "$failures" "$warnings"
