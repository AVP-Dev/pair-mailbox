#!/usr/bin/env bash
set -euo pipefail

usage() {
  printf '%s\n' "Использование: $0" \
    "Сканирует tracked и неигнорируемые untracked файлы проекта на вероятные секреты." \
    "Код возврата: 0 — чисто, 1 — findings, 2 — ошибка сканера."
}

case "${1:-}" in
  --help|-h|"")
    if [ "$#" -gt 0 ] && [ "${1:-}" != "" ]; then
      usage
      exit 0
    fi
    ;;
  *)
    printf 'Неизвестная опция: %s\n' "$1" >&2
    usage >&2
    exit 2
    ;;
esac

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT"

PRIVATE_KEY_PATTERN='-----BEGIN ([A-Z0-9 ]+ )?PRIVATE KEY-----'
AWS_ACCESS_KEY_PATTERN='AKIA[0-9A-Z]{16}'
GOOGLE_API_KEY_PATTERN='AIza[0-9A-Za-z_-]{35}'
DATABASE_URL_PATTERN='(postgres(ql)?|mysql|mongodb(\+srv)?|redis)://[^[:space:]]+:[^[:space:]@]+@'
SENSITIVE_ASSIGNMENT_PATTERN="(password|passwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token|private[_-]?key)[[:space:]]*[:=][[:space:]]*['\"]?[A-Za-z0-9_./+=-]{16,}"

findings=0

report() {
  findings=$((findings + 1))
  printf 'SECURITY_FINDING %s:%s %s\n' "$1" "$2" "$3"
}

skip_file() {
  case "$1" in
    .git/*) return 0 ;;
    *) return 1 ;;
  esac
}

scan_pattern() {
  local file="$1"
  local category="$2"
  local pattern="$3"
  local status=0
  local matches
  matches="$(LC_ALL=C grep -nE -I -- "$pattern" "$file" 2>/dev/null)" || status=$?
  if [ "$status" -gt 1 ]; then
    printf 'security-scan: не удалось прочитать %s\n' "$file" >&2
    return 2
  fi
  if [ "$status" -eq 0 ]; then
    while IFS= read -r match; do
      line_number="${match%%:*}"
      [ -n "$line_number" ] || continue
      report "$file" "$line_number" "$category"
    done <<<"$matches"
  fi
}

scan_file() {
  local file="$1"
  skip_file "$file" && return 0
  [ -L "$file" ] && return 0
  [ -f "$file" ] || return 0
  if [ ! -r "$file" ]; then
    printf 'security-scan: файл недоступен для чтения: %s\n' "$file" >&2
    return 2
  fi

  case "${file##*/}" in
    .env)
      report "$file" 1 "environment-file"
      ;;
    .env.*)
      case "${file##*/}" in
        .env.example|.env.sample|.env.template) ;;
        *) report "$file" 1 "environment-file" ;;
      esac
      ;;
  esac

  scan_pattern "$file" "private-key-material" "$PRIVATE_KEY_PATTERN" || return 2
  scan_pattern "$file" "aws-access-key" "$AWS_ACCESS_KEY_PATTERN" || return 2
  scan_pattern "$file" "google-api-key" "$GOOGLE_API_KEY_PATTERN" || return 2
  scan_pattern "$file" "credentialed-database-url" "$DATABASE_URL_PATTERN" || return 2
  scan_pattern "$file" "sensitive-assignment" "$SENSITIVE_ASSIGNMENT_PATTERN" || return 2
}

if command -v git >/dev/null 2>&1 && git -C "$PROJECT_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  while IFS= read -r -d '' file; do
    scan_file "$file" || exit $?
  done < <(git -C "$PROJECT_ROOT" ls-files -z --cached --others --exclude-standard)
else
  while IFS= read -r -d '' file; do
    scan_file "$file" || exit $?
  done < <(find . \( -type d \( -name .git -o -name node_modules -o -name vendor -o -name .venv -o -name venv -o -name dist -o -name build -o -name coverage -o -name .next -o -name target \) -prune \) -o -type f -print0)
fi

if [ "$findings" -gt 0 ]; then
  printf 'security-scan: найдено %s потенциальных finding(s)\n' "$findings" >&2
  exit 1
fi

printf 'security-scan: clean\n'
