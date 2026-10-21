#!/usr/bin/env bash
# verify-lock.sh — one gate per worktree, and the loser is told.
#
# Why: every gate run writes the SAME artifacts — `.pair/verify-<name>.log` for
# each check and `.pair/verify-results.txt` for the totals — and a run REMOVES
# both at its start. Two runners in one worktree therefore do not merely race:
# the second one deletes the first one's results, and each run's totals count
# the other's rows. Round 137 measured it: 83 result rows where 78 checks had
# run, and a "0 required failures" that belonged to neither run.
#
# The lock is a directory (mkdir is atomic on every POSIX filesystem) holding
# the owner's pid. A dead pid means a crashed run: the lock is stale and the
# next run takes it over — a lock that outlives its owner would block the room
# forever, which is worse than the race it prevents.
#
# Usage:
#   . "$(dirname "$0")/verify-lock.sh"
#   acquire_worktree_lock "$PROJECT_ROOT" || exit 75
#   trap 'release_worktree_lock "$PROJECT_ROOT"' EXIT
#
# Re-entrancy: a parent that already holds the lock hands its TOKEN to its
# children (verify-changed.sh runs `verify-project.sh --only static`), and a
# child that sees its own token in the lock does not take it a second time.
# The token, not a boolean: a boolean leaks into every descendant - including
# the unrelated processes a suite spawns - and then those processes believe
# they hold a lock they never took. A token matches only the lock it was
# issued for, so a leaked value is ignored.

VERIFY_LOCK_DIRNAME="verify.lock.d"
VERIFY_LOCK_ENV="VERIFY_WORKTREE_LOCK"

# Where the lock for $1 (the project root) lives.
verify_lock_dir() {
  printf '%s\n' "$1/.pair/$VERIFY_LOCK_DIRNAME"
}

# A token unique to this take.
verify_lock_token() {
  printf '%s-%s-%s\n' "$$" "$(date +%s%N 2>/dev/null || date +%s)" "$RANDOM"
}

# Take the lock for $1. 0 = taken (or already ours), 1 = a live owner holds it.
acquire_worktree_lock() {
  local root="$1" dir pid held mine
  mine="${!VERIFY_LOCK_ENV:-}"
  dir="$(verify_lock_dir "$root")"
  mkdir -p "$(dirname "$dir")" 2>/dev/null || true
  if mkdir "$dir" 2>/dev/null; then
    mine="$(verify_lock_token)"
    printf '%s\n' "$mine" >"$dir/token"
    printf '%s\n' "$$" >"$dir/pid"
    export "$VERIFY_LOCK_ENV=$mine"
    return 0
  fi
  # The lock exists. Is it ours?
  held=""
  if [ -f "$dir/token" ]; then
    held="$(cat "$dir/token" 2>/dev/null || true)"
  fi
  if [ -n "$mine" ] && [ "$mine" = "$held" ]; then
    return 0
  fi
  pid=""
  if [ -f "$dir/pid" ]; then
    pid="$(cat "$dir/pid" 2>/dev/null || true)"
  fi
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    return 1
  fi
  # Stale: the owner is gone. Take it over.
  rm -rf "$dir" 2>/dev/null || true
  if mkdir "$dir" 2>/dev/null; then
    mine="$(verify_lock_token)"
    printf '%s\n' "$mine" >"$dir/token"
    printf '%s\n' "$$" >"$dir/pid"
    export "$VERIFY_LOCK_ENV=$mine"
    return 0
  fi
  return 1
}

# Free the lock for $1. Only the owner frees it: the lock carries the owner's
# pid, and a child that inherited the token does not match it.
release_worktree_lock() {
  local root="$1" dir pid
  dir="$(verify_lock_dir "$root")"
  [ -d "$dir" ] || return 0
  pid=""
  if [ -f "$dir/pid" ]; then
    pid="$(cat "$dir/pid" 2>/dev/null || true)"
  fi
  if [ -n "$pid" ] && [ "$pid" != "$$" ]; then
    return 0
  fi
  rm -rf "$dir" 2>/dev/null || true
}
