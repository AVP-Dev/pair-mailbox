#!/bin/sh
# Shell syntax gate: every shell script in the repo must parse.
#
# Why this exists (round 127): the shell-syntax leg of the verification profile
# was a hand-written list of 11 files, while the repository tracks 32 shell
# scripts. A syntax-breaking edit to any of the other 21 — including
# scripts/verify-changed.sh, the script that decides which suites a change
# runs — left the static gate green. Same class as the python side (round 104
# S1): a check whose membership is written by hand names fewer files than
# exist, and its green claims more than it checked.
#
# Membership is DERIVED, never listed: every git-tracked file whose shebang
# names a shell is parsed with that shell. A new shell script is covered the
# moment it is added; a removed one drops out with no edit here.
#
# The interpreter comes from the shebang, because the two differ in this repo
# and a file parsed by the wrong one reports errors that are not there:
# scripts/verify-project.sh is `#!/usr/bin/env bash` and uses bash-only
# syntax, so `sh -n` on it fails on a VALID file. Files that declare `sh` are
# parsed with `sh` (dash here) and files that declare bash with `bash`.
#
# Exit 0 all parse; 1 any file fails to parse, naming each one.
#
# `--list` prints the files this leg checks, one per line, and exits 0. The
# guard test reads that list to check the leg's membership against the claim
# (mcp/pair_mailbox/test_verification_profile.py).
set -eu

cd "$(dirname "$0")/.."

list_only=0
case "${1:-}" in
    "") ;;
    --list) list_only=1 ;;
    *) echo "usage: $0 [--list]" >&2; exit 2 ;;
esac

status=0
count=0
for f in $(git ls-files); do
    case "$f" in *__pycache__*) continue ;; esac
    first="$(head -1 "$f" 2>/dev/null || true)"
    case "$first" in
        '#!'*bash*) interp=bash ;;
        '#!'*/sh|'#!'*sh) interp=sh ;;
        *) continue ;;
    esac
    count=$((count + 1))
    if [ "$list_only" -eq 1 ]; then
        echo "$f"
        continue
    fi
    if ! "$interp" -n "$f" 2>/dev/null; then
        echo "SYNTAX FAIL: $f"
        "$interp" -n "$f" 2>&1 | tail -3
        status=1
    fi
done

if [ "$list_only" -eq 1 ]; then
    exit 0
fi

if [ "$status" -eq 0 ]; then
    echo "shell-syntax: $count file(s) parse"
else
    echo "shell-syntax: FAILED ($count file(s) checked)" >&2
fi
exit "$status"
