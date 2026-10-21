#!/usr/bin/env bash
set -euo pipefail

usage() {
  printf '%s\n' "Usage: $0 [--profile PATH] [--only LAYER[,LAYER...]] [--list] [--dry-run] [--jobs N]" \
    "  --profile PATH  the command profile relative to the project root" \
    "  --only LAYERS   run only the given layers" \
    "  --list          show the profile with no launch" \
    "  --dry-run       show the selected checks with no launch" \
  "  --jobs N        parallel checks (default 1; suites isolated: temp-state, unique tmux names; live-tmux serial-marked suites always run alone)"
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
      [ "$#" -ge 2 ] || fail "--profile needs a path"
      PROFILE_PATH="$2"
      shift 2
      ;;
    --only)
      [ "$#" -ge 2 ] || fail "--only needs a layer list"
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
      [ "$#" -ge 2 ] || fail "--jobs needs a number"
      [[ "$2" =~ ^[1-9][0-9]*$ ]] || fail "--jobs: needs a positive number, got '$2'"
      JOBS="$2"
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      printf 'Unknown option: %s\n' "$1" >&2
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
    ,*|*,|*,,*) fail "an empty layer in --only" ;;
  esac
  IFS=',' read -r -a requested_layers <<<"$ONLY"
  [ "${#requested_layers[@]}" -gt 0 ] || fail "empty --only"
  for layer in "${requested_layers[@]}"; do
    valid_layer "$layer" || fail "unknown layer: $layer"
  done
fi

[ -f "$PROFILE_FILE" ] || fail "profile not found: $PROFILE_FILE"
[ -r "$PROFILE_FILE" ] || fail "profile not readable: $PROFILE_FILE"

cd "$PROJECT_ROOT"

found=0
selected=0
failures=0
warnings=0
line_number=0
running=0
mkdir -p "$PROJECT_ROOT/.pair"
# One gate per worktree (scripts/verify-lock.sh). This run removes the logs
# and the results file below, so a second runner in the same worktree would
# delete this run's evidence and count the other's rows - round 137 measured
# it: 83 result rows where 78 checks had run, and a "0 required failures"
# that belonged to neither run. The loser is told, not left to corrupt both.
. "$SCRIPT_DIR/verify-lock.sh"
if ! acquire_worktree_lock "$PROJECT_ROOT"; then
  printf 'verify-project: another gate is running in this worktree (%s)\n' \
    "$(verify_lock_dir "$PROJECT_ROOT")" >&2
  printf 'verify-project: refusing to run - two gates in one worktree delete each other'"'"'s logs and results\n' >&2
  exit 75
fi
trap 'release_worktree_lock "$PROJECT_ROOT"' EXIT
rm -f "$PROJECT_ROOT/.pair/verify-results.txt" "$PROJECT_ROOT/.pair/verify-"*.log

while IFS=$'\t' read -r name layer required command serial extra || [ -n "${name:-}${layer:-}${required:-}${command:-}${serial:-}${extra:-}" ]; do
  line_number=$((line_number + 1))
  name="${name%$'\r'}"
  layer="${layer%$'\r'}"
  required="${required%$'\r'}"
  command="${command%$'\r'}"
  serial="${serial%$'\r'}"
  extra="${extra%$'\r'}"

  [ -n "$name" ] || continue
  case "$name" in
    \#*) continue ;;
  esac

  [ -z "$extra" ] || fail "line $line_number: extra fields"
  [[ "$name" =~ ^[A-Za-z0-9._-]+$ ]] || fail "line $line_number: bad check name"
  valid_layer "$layer" || fail "line $line_number: unknown layer '$layer'"
  case "$required" in
    0|1) ;;
    *) fail "line $line_number: required must be 0 or 1" ;;
  esac
  case "$serial" in
    ""|serial) ;;
    *) fail "line $line_number: serial must be empty or 'serial'" ;;
  esac
  case ";$seen_names;" in
    *";$name;"*) fail "line $line_number: duplicate check name '$name'" ;;
  esac
  seen_names="$seen_names;$name"
  found=1

  if [ "$LIST_ONLY" -eq 1 ]; then
    if selected_layer "$layer"; then
      printf '%s\t%s\t%s\t%s\t%s\n' "$name" "$layer" "$required" "$command" "$serial"
    else
      printf '%s\t%s\t%s\t%s\t%s\tskipped\n' "$name" "$layer" "$required" "$command" "$serial"
    fi
    continue
  fi

  selected_layer "$layer" || continue
  selected=1

  if [ "$DRY_RUN" -eq 1 ]; then
    printf '[verify] dry-run: %s\n' "$command"
    continue
  fi

  # Live-tmux suites (the serial mark in the profile) share the host tmux state:
  # two runners see foreign panes falling over each other (the backlog
  # live-tmux-parallel-flake; TestRestart caught the live pane %7). Past
  # JOBS>1 such a check awaits the batch tail, runs sync, and only then the batch
  # goes on — in proud solitude.
  if [ "$serial" = "serial" ] && [ "$JOBS" -gt 1 ]; then
    wait
    running=0
    printf '[verify] %s (%s) serial-only\n' "$name" "$layer"
    log="$PROJECT_ROOT/.pair/verify-$name.log"
    if bash -c "$command" >"$log" 2>&1; then
      echo "0 $required $name" >>"$PROJECT_ROOT/.pair/verify-results.txt"
    else
      echo "$? $required $name" >>"$PROJECT_ROOT/.pair/verify-results.txt"
    fi
    continue
  fi
  # Parallelism in batches of JOBS: every check writes its own log, the total
  # gathers past wait. No suite output interleaves with a foreign one.
  printf '[verify] %s (%s)\n' "$name" "$layer"
  log="$PROJECT_ROOT/.pair/verify-$name.log"
  # set +e inside the subshell: it inherits the parent errexit, and a falling check
  # killed the subshell BEFORE writing the result — then the total either broke
  # silent on wait -n (jobs=1) or reduced as "0 failures" (jobs>1): a red required
  # check read green.
  ( set +e; bash -c "$command" >"$log" 2>&1; echo "$? $required $name" >>"$PROJECT_ROOT/.pair/verify-results.txt" ) &
  running=$((running + 1))
  if [ "$running" -ge "$JOBS" ]; then
    wait -n
    running=$((running - 1))
  fi
done <"$PROFILE_FILE"

# Draining the batch tail, reducing totals from the results file.
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

[ "$found" -eq 1 ] || fail "the profile holds no checks"

if [ "$LIST_ONLY" -eq 1 ]; then
  exit 0
fi

[ "$selected" -eq 1 ] || fail "no checks selected to run"

if [ "$failures" -gt 0 ]; then
  printf '[verify] failed: %s required check(s), %s warning(s)\n' "$failures" "$warnings" >&2
  exit 1
fi

printf '[verify] passed: %s required failure(s), %s warning(s)\n' "$failures" "$warnings"
