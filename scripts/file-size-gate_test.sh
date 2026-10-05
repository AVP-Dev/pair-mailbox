#!/usr/bin/env bash
#
# file-size-gate_test.sh — проверки рэтчета размера файла.
#
# Гейт стоит на пути к каждому коммиту, поэтому тестируется так же строго, как
# всё остальное: не «скрипт запускается», а «каждый его отказ срабатывает по
# назначению». Молчаливый гейт хуже отсутствующего — он создаёт ложное
# ощущение, что размер контролируется.
#
# Запуск: bash scripts/file-size-gate_test.sh

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
GATE="$ROOT/scripts/file-size-gate.sh"
TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/file-size-gate-tests.XXXXXX")"
trap 'rm -rf "$TMP_ROOT"' EXIT

passed=0

fail() {
  printf 'FAIL: %s\n' "$*" >&2
  exit 1
}

assert_status() {
  expected="$1"
  shift
  set +e
  "$@" >"$TMP_ROOT/out" 2>&1
  actual=$?
  set -e
  [ "$actual" -eq "$expected" ] || {
    printf 'Expected exit %s, got %s\n' "$expected" "$actual" >&2
    printf '%s\n' '--- output ---' >&2
    cat "$TMP_ROOT/out" >&2
    exit 1
  }
  passed=$((passed + 1))
}

assert_contains() {
  grep -qF -- "$1" "$TMP_ROOT/out" || {
    printf 'Expected output to contain: %s\n' "$1" >&2
    printf '%s\n' '--- output ---' >&2
    cat "$TMP_ROOT/out" >&2
    exit 1
  }
  passed=$((passed + 1))
}

assert_not_contains() {
  if grep -qF -- "$1" "$TMP_ROOT/out"; then
    printf 'Did not expect output to contain: %s\n' "$1" >&2
    cat "$TMP_ROOT/out" >&2
    exit 1
  fi
  passed=$((passed + 1))
}

# Изолированный проект с одним файлом, чтобы проверки не зависели от
# настоящего репозитория и не ломались, когда в нём меняются размеры.
make_sandbox() {
  name="$1"
  root="$TMP_ROOT/$name"
  mkdir -p "$root/scripts" "$root/src"
  cp "$GATE" "$root/scripts/file-size-gate.sh"
  chmod +x "$root/scripts/file-size-gate.sh"
  git -C "$root" init -q
  printf '%s\n' "$root"
}

# --- 1. Свежий проект без исключений проходит ---------------------------------
SANDBOX="$(make_sandbox clean)"
printf 'x = 1\n' >"$SANDBOX/src/small.py"
printf 'path\tmax_lines\treason\n' >"$SANDBOX/scripts/file-size-baseline.tsv"
printf '# нет ни одного исключения\n' >>"$SANDBOX/scripts/file-size-baseline.tsv"
assert_status 2 "$SANDBOX/scripts/file-size-gate.sh"
assert_contains "не содержит ни одной записи"

# --- 2. Файл в норме проходит --------------------------------------------------
SANDBOX="$(make_sandbox ok)"
printf 'path\tmax_lines\treason\nsrc/small.py\t100\tпричина\n' \
  >"$SANDBOX/scripts/file-size-baseline.tsv"
printf 'x = 1\n' >"$SANDBOX/src/small.py"
assert_status 0 "$SANDBOX/scripts/file-size-gate.sh"
assert_contains "clean"

# --- 3. Файл вырос на строку — рэтчет срабатывает ------------------------------
SANDBOX="$(make_sandbox grown)"
printf 'path\tmax_lines\treason\nsrc/small.py\t10\tпричина\n' \
  >"$SANDBOX/scripts/file-size-baseline.tsv"
for i in $(seq 1 11); do printf 'line %s\n' "$i" >>"$SANDBOX/src/small.py"; done
assert_status 1 "$SANDBOX/scripts/file-size-gate.sh"
assert_contains "рост запрещён"

# --- 4. Файл ужался — запись протухла, рэтчет требует опустить cap --------------
# Именно «перестал нуждаться в исключении»: cap выше жёсткого порога, а файл
# ушёл под него. Раньше проверка сравнивала cap с текущим размером напрямую, и
# тогда ЛЮБАЯ правка файла требовала правки baseline — тест поймал это сразу.
SANDBOX="$(make_sandbox shrunk)"
printf 'path\tmax_lines\treason\nsrc/small.py\t900\tбыло исключение\n' \
  >"$SANDBOX/scripts/file-size-baseline.tsv"
printf 'x = 1\n' >"$SANDBOX/src/small.py"
assert_status 1 "$SANDBOX/scripts/file-size-gate.sh"
assert_contains "протухла"
assert_contains "опусти cap до 1"

# --- 4a. Cap это потолок, а не точное значение: файл может быть намного меньше --
SANDBOX="$(make_sandbox ceiling)"
printf 'path\tmax_lines\treason\nsrc/small.py\t700\tпотолок\n' \
  >"$SANDBOX/scripts/file-size-baseline.tsv"
printf 'x = 1\n' >"$SANDBOX/src/small.py"
assert_status 0 "$SANDBOX/scripts/file-size-gate.sh"

# --- 5. Новый файл выше жёсткого порога без исключения -------------------------
SANDBOX="$(make_sandbox huge)"
printf 'path\tmax_lines\treason\nsrc/small.py\t100\tпричина\n' \
  >"$SANDBOX/scripts/file-size-baseline.tsv"
printf 'x = 1\n' >"$SANDBOX/src/small.py"
for i in $(seq 1 801); do printf 'line %s\n' "$i" >>"$SANDBOX/src/huge.py"; done
assert_status 1 "$SANDBOX/scripts/file-size-gate.sh"
assert_contains "жёсткий порог 800"

# --- 6. Файл выше порога, но зафиксированный исключением, проходит -------------
SANDBOX="$(make_sandbox baselined)"
printf 'path\tmax_lines\treason\nsrc/small.py\t100\tпричина\nsrc/huge.py\t900\tзафиксировано\n' \
  >"$SANDBOX/scripts/file-size-baseline.tsv"
printf 'x = 1\n' >"$SANDBOX/src/small.py"
for i in $(seq 1 801); do printf 'line %s\n' "$i" >>"$SANDBOX/src/huge.py"; done
assert_status 0 "$SANDBOX/scripts/file-size-gate.sh"

# --- 7. Исключение без причины не считается зафиксированным --------------------
SANDBOX="$(make_sandbox noreason)"
printf 'path\tmax_lines\treason\nsrc/small.py\t100\t\n' \
  >"$SANDBOX/scripts/file-size-baseline.tsv"
printf 'x = 1\n' >"$SANDBOX/src/small.py"
assert_status 2 "$SANDBOX/scripts/file-size-gate.sh"
assert_contains "reason обязателен"

# --- 8. Baseline с лишним полем — ошибка формата, а не тихий провал ------------
SANDBOX="$(make_sandbox extra)"
printf 'path\tmax_lines\treason\nsrc/small.py\t100\tпричина\tлишнее\n' \
  >"$SANDBOX/scripts/file-size-baseline.tsv"
printf 'x = 1\n' >"$SANDBOX/src/small.py"
assert_status 2 "$SANDBOX/scripts/file-size-gate.sh"
assert_contains "лишние поля"

# --- 9. Нечисловой cap — ошибка формата ---------------------------------------
SANDBOX="$(make_sandbox badcap)"
printf 'path\tmax_lines\treason\nsrc/small.py\tмного\tпричина\n' \
  >"$SANDBOX/scripts/file-size-baseline.tsv"
printf 'x = 1\n' >"$SANDBOX/src/small.py"
assert_status 2 "$SANDBOX/scripts/file-size-gate.sh"
assert_contains "max_lines"

# --- 10. Запись на несуществующий файл — протухшая запись ----------------------
SANDBOX="$(make_sandbox ghost)"
printf 'path\tmax_lines\treason\nsrc/small.py\t100\tпричина\nsrc/gone.py\t100\tустарело\n' \
  >"$SANDBOX/scripts/file-size-baseline.tsv"
printf 'x = 1\n' >"$SANDBOX/src/small.py"
assert_status 1 "$SANDBOX/scripts/file-size-gate.sh"
assert_contains "отсутствующий файл"

# --- 11. Дубликат пути в baseline ---------------------------------------------
SANDBOX="$(make_sandbox dup)"
printf 'path\tmax_lines\treason\nsrc/small.py\t100\tраз\nsrc/small.py\t200\tдва\n' \
  >"$SANDBOX/scripts/file-size-baseline.tsv"
printf 'x = 1\n' >"$SANDBOX/src/small.py"
assert_status 2 "$SANDBOX/scripts/file-size-gate.sh"
assert_contains "повторяющийся путь"

# --- 12. Отсутствующий baseline — ошибка, а не «всё чисто» ---------------------
SANDBOX="$(make_sandbox nobaseline)"
printf 'x = 1\n' >"$SANDBOX/src/small.py"
rm -f "$SANDBOX/scripts/file-size-baseline.tsv"
assert_status 2 "$SANDBOX/scripts/file-size-gate.sh"
assert_contains "baseline не найден"

# --- 13. Шебеллы и markdown не измеряются: область гейта объявлена узкой ------
SANDBOX="$(make_sandbox scope)"
printf 'path\tmax_lines\treason\nsrc/small.py\t100\tпричина\n' \
  >"$SANDBOX/scripts/file-size-baseline.tsv"
printf 'x = 1\n' >"$SANDBOX/src/small.py"
for i in $(seq 1 900); do printf 'строка %s\n' "$i" >>"$SANDBOX/README.md"; done
for i in $(seq 1 900); do printf 'строка %s\n' "$i" >>"$SANDBOX/src/big.sh"; done
assert_status 1 "$SANDBOX/scripts/file-size-gate.sh"
assert_contains "src/big.sh"
assert_not_contains "README.md"

# --- 14. Настоящий baseline проекта не протух и самосогласован ---------------
assert_status 0 "$GATE"
passed=$((passed + 1))

printf 'file-size-gate: %s проверок пройдено\n' "$passed"