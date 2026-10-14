#!/usr/bin/env bash
#
# file-size-gate_test.sh — the file-size ratchet checks.
#
# The gate stands on the path to every commit, so it tests as strict as everything
# else: not "the script launches" but "every refusal fires as intended". A silent
# gate reads worse than a missing one — it builds a false feeling that size is
# controlled.
#
# Run: bash scripts/file-size-gate_test.sh

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

# An isolated project with one file, so checks never depend on the real repo, never
# breaking when its sizes move.
make_sandbox() {
  name="$1"
  root="$TMP_ROOT/$name"
  mkdir -p "$root/scripts" "$root/src"
  cp "$GATE" "$root/scripts/file-size-gate.sh"
  chmod +x "$root/scripts/file-size-gate.sh"
  git -C "$root" init -q
  printf '%s\n' "$root"
}

# --- 1. A fresh project with no exceptions passes -------------------------------
SANDBOX="$(make_sandbox clean)"
printf 'x = 1\n' >"$SANDBOX/src/small.py"
printf 'path\tmax_lines\treason\n' >"$SANDBOX/scripts/file-size-baseline.tsv"
printf '# no exceptions at all\n' >>"$SANDBOX/scripts/file-size-baseline.tsv"
assert_status 2 "$SANDBOX/scripts/file-size-gate.sh"
assert_contains "holds no records"

# --- 2. A file inside the norm passes --------------------------------------------
SANDBOX="$(make_sandbox ok)"
printf 'path\tmax_lines\treason\nsrc/small.py\t100\treason\n' \
  >"$SANDBOX/scripts/file-size-baseline.tsv"
printf 'x = 1\n' >"$SANDBOX/src/small.py"
assert_status 0 "$SANDBOX/scripts/file-size-gate.sh"
assert_contains "clean"

# --- 3. The file grew a line — the ratchet fires ---------------------------------
SANDBOX="$(make_sandbox grown)"
printf 'path\tmax_lines\treason\nsrc/small.py\t10\treason\n' \
  >"$SANDBOX/scripts/file-size-baseline.tsv"
for i in $(seq 1 11); do printf 'line %s\n' "$i" >>"$SANDBOX/src/small.py"; done
assert_status 1 "$SANDBOX/scripts/file-size-gate.sh"
assert_contains "growth banned"

# --- 4. The file shrank — the record went stale, the ratchet demands lowering cap -
# Exactly "stopped needing the exception": cap above the hard cap, while the file
# dropped under it. The check used to compare cap against the current size directly,
# and then ANY file edit demanded a baseline edit — the test caught it at once.
SANDBOX="$(make_sandbox shrunk)"
printf 'path\tmax_lines\treason\nsrc/small.py\t900\twas an exception\n' \
  >"$SANDBOX/scripts/file-size-baseline.tsv"
printf 'x = 1\n' >"$SANDBOX/src/small.py"
assert_status 1 "$SANDBOX/scripts/file-size-gate.sh"
assert_contains "went stale"
assert_contains "drop cap to 1"

# --- 4a. Cap is a ceiling, not an exact value: the file may run far smaller --------
SANDBOX="$(make_sandbox ceiling)"
printf 'path\tmax_lines\treason\nsrc/small.py\t700\tceiling\n' \
  >"$SANDBOX/scripts/file-size-baseline.tsv"
printf 'x = 1\n' >"$SANDBOX/src/small.py"
assert_status 0 "$SANDBOX/scripts/file-size-gate.sh"

# --- 5. A new file past the hard cap with no exception ---------------------------
SANDBOX="$(make_sandbox huge)"
printf 'path\tmax_lines\treason\nsrc/small.py\t100\treason\n' \
  >"$SANDBOX/scripts/file-size-baseline.tsv"
printf 'x = 1\n' >"$SANDBOX/src/small.py"
for i in $(seq 1 801); do printf 'line %s\n' "$i" >>"$SANDBOX/src/huge.py"; done
assert_status 1 "$SANDBOX/scripts/file-size-gate.sh"
assert_contains "hard cap 800"

# --- 6. A file past the cap but pinned by an exception passes --------------------
SANDBOX="$(make_sandbox baselined)"
printf 'path\tmax_lines\treason\nsrc/small.py\t100\treason\nsrc/huge.py\t900\tpinned\n' \
  >"$SANDBOX/scripts/file-size-baseline.tsv"
printf 'x = 1\n' >"$SANDBOX/src/small.py"
for i in $(seq 1 801); do printf 'line %s\n' "$i" >>"$SANDBOX/src/huge.py"; done
assert_status 0 "$SANDBOX/scripts/file-size-gate.sh"

# --- 7. A reasonless exception never counts as pinned ----------------------------
SANDBOX="$(make_sandbox noreason)"
printf 'path\tmax_lines\treason\nsrc/small.py\t100\t\n' \
  >"$SANDBOX/scripts/file-size-baseline.tsv"
printf 'x = 1\n' >"$SANDBOX/src/small.py"
assert_status 2 "$SANDBOX/scripts/file-size-gate.sh"
assert_contains "reason is mandatory"

# --- 8. A baseline with an extra field is a format error, never a silent fall ----
SANDBOX="$(make_sandbox extra)"
printf 'path\tmax_lines\treason\nsrc/small.py\t100\treason\textra\n' \
  >"$SANDBOX/scripts/file-size-baseline.tsv"
printf 'x = 1\n' >"$SANDBOX/src/small.py"
assert_status 2 "$SANDBOX/scripts/file-size-gate.sh"
assert_contains "extra fields"

# --- 9. A non-numeric cap is a format error ---------------------------------------
SANDBOX="$(make_sandbox badcap)"
printf 'path\tmax_lines\treason\nsrc/small.py\tmany\treason\n' \
  >"$SANDBOX/scripts/file-size-baseline.tsv"
printf 'x = 1\n' >"$SANDBOX/src/small.py"
assert_status 2 "$SANDBOX/scripts/file-size-gate.sh"
assert_contains "max_lines"

# --- 10. A record on a missing file is a stale record ----------------------------
SANDBOX="$(make_sandbox ghost)"
printf 'path\tmax_lines\treason\nsrc/small.py\t100\treason\nsrc/gone.py\t100\tstale\n' \
  >"$SANDBOX/scripts/file-size-baseline.tsv"
printf 'x = 1\n' >"$SANDBOX/src/small.py"
assert_status 1 "$SANDBOX/scripts/file-size-gate.sh"
assert_contains "missing file"

# --- 11. A duplicate path in baseline --------------------------------------------
SANDBOX="$(make_sandbox dup)"
printf 'path\tmax_lines\treason\nsrc/small.py\t100\tone\nsrc/small.py\t200\ttwo\n' \
  >"$SANDBOX/scripts/file-size-baseline.tsv"
printf 'x = 1\n' >"$SANDBOX/src/small.py"
assert_status 2 "$SANDBOX/scripts/file-size-gate.sh"
assert_contains "duplicate path"

# --- 12. A missing baseline is an error, never "all clean" -----------------------
SANDBOX="$(make_sandbox nobaseline)"
printf 'x = 1\n' >"$SANDBOX/src/small.py"
rm -f "$SANDBOX/scripts/file-size-baseline.tsv"
assert_status 2 "$SANDBOX/scripts/file-size-gate.sh"
assert_contains "baseline not found"

# --- 13. Shebangs and markdown never measure: the gate scope declares narrow -----
SANDBOX="$(make_sandbox scope)"
printf 'path\tmax_lines\treason\nsrc/small.py\t100\treason\n' \
  >"$SANDBOX/scripts/file-size-baseline.tsv"
printf 'x = 1\n' >"$SANDBOX/src/small.py"
for i in $(seq 1 900); do printf 'line %s\n' "$i" >>"$SANDBOX/README.md"; done
for i in $(seq 1 900); do printf 'line %s\n' "$i" >>"$SANDBOX/src/big.sh"; done
assert_status 1 "$SANDBOX/scripts/file-size-gate.sh"
assert_contains "src/big.sh"
assert_not_contains "README.md"

# --- 14. The project real baseline never went stale, self-consistent -------------
assert_status 0 "$GATE"
passed=$((passed + 1))

printf 'file-size-gate: %s checks passed\n' "$passed"