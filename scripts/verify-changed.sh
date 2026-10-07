#!/usr/bin/env bash
# verify-changed: static + только затронутые сюиты (5.5 мин → секунды).
# Использование: ./scripts/verify-changed.sh [--base REF] [--all]
#   --base REF  diff против REF (по умолчанию — незакоммиченное + HEAD)
#   --all       показать выбор без запуска
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
ROOT="$(dirname "$SCRIPT_DIR")"
MAP="$SCRIPT_DIR/verify-map.tsv"
BASE=""
DRY=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --base) BASE="${2:-}"; shift 2 || { echo "error: --base needs REF" >&2; exit 2; } ;;
    --all|--dry-run) DRY=1; shift ;;
    *) echo "usage: $0 [--base REF] [--all]" >&2; exit 2 ;;
  esac
done
cd "$ROOT"
if [ -n "$BASE" ]; then
  files=$(git diff --name-only "$BASE" -- . ':!node_modules')
else
  files=$( (git diff --name-only -- . ':!node_modules'; git ls-files --others --exclude-standard) | sort -u)
fi
[ -n "$files" ] || { echo "verify-changed: нечего проверять, дерево чистое"; exit 0; }
suites=""
while IFS= read -r f; do
  [ -n "$f" ] || continue
  suite=$(awk -F'\t' -v f="$f" '$f == $1 || index($f, $1"/") == 1 {print $2; found=1} END {if (!found) print "UNKNOWN"}' "$MAP" | head -1)
  # Точное совпадение бьёт префикс: awk выше отдаёт первое совпавшее —
  # файл scripts/x.sh совпадёт и с scripts/, и со своей строкой; точное ищем сами.
  exact=$(awk -F'\t' -v f="$f" '$1 == f {print $2}' "$MAP" | head -1)
  [ -n "$exact" ] && suite="$exact"
  case ";$suites;" in *";$suite;"*) ;; *) suites="$suites;$suite" ;; esac
done <<< "$files"
suites="${suites#;}"
echo "verify-changed: файлы: $(echo "$files" | tr '\n' ' ')"
echo "verify-changed: сюиты: $suites"
[ "$DRY" -eq 1 ] && exit 0
# static всегда: file-size-gate и comment-lint ловят то, что не видит ни одна сюита.
"$SCRIPT_DIR/verify-project.sh" --only static || exit $?
fail=0
for s in $(echo "$suites" | tr ';' ' '); do
  case "$s" in
    UNKNOWN) echo "verify-changed: UNKNOWN — маппинга нет, гоню весь positive" >&2
      "$SCRIPT_DIR/verify-project.sh" --only positive || fail=1; break ;;
    installer-subset) echo "verify-changed: [installer-subset] PAIR_INSTALL_FAST=1 python3 install_test.py"
      PAIR_INSTALL_FAST=1 python3 install_test.py || { echo "verify-changed: installer-subset FAILED" >&2; fail=1; } ;;
    *-check|docs-check) echo "verify-changed: $s — отдельной сюиты нет, покрыто static" ;;
    *) cmd=$(awk -F'\t' -v s="$s" '$1 == s {print $4; exit}' "$SCRIPT_DIR/verification-profile.tsv")
      if [ -z "$cmd" ]; then echo "verify-changed: сюита $s не найдена в профиле" >&2; fail=1; continue; fi
      echo "verify-changed: [$s] $cmd"
      bash -c "$cmd" || { echo "verify-changed: $s FAILED" >&2; fail=1; } ;;
  esac
done
exit $fail
