#!/bin/sh
# Python syntax gate: every python file in the repo must parse.
#
# Why this exists (round 104 S1): the shell-syntax leg runs `bash -n` over a
# hand-listed set of files, and two Python files of exactly that shape —
# bin/pairpilot, bin/pairrestart — were absent from every row of the profile.
# A syntax-breaking edit to either passed the whole gate silently. The class
# is the round's own: a check whose membership is written by hand names fewer
# files than exist, and its green claims more than it checked.
#
# Membership is DERIVED, never listed: every git-tracked file that is a python
# file gets compile()d. A new python file is covered the moment it is added;
# a removed file drops out with no edit here. The hand-written list was the
# defect, so there is no hand-written list — and the same applies to a GLOB
# TRío, round 104's second proof: the first version swept bin/, mcp/pair_mailbox/
# and scripts/ and missed three python files at the repo root, install.sh and
# finish.sh among them — python files carrying .sh names, exactly this leg's
# shape. The sweep is repo-wide, over every tracked path.
#
# A python file is a tracked .py module OR a tracked script whose shebang says
# python. The shebang alone was not enough: it covered 65 of the repo's 141
# python files, because 76 of them are library modules with no shebang at all.
# A syntax error in mcp/pair_mailbox/store_core.py left this gate green while
# it printed "65 file(s) parse" (round 127).
#
# Exit 0 all parse; 1 any file fails to compile, naming each one.
#
# `--list` prints the files this leg checks, one per line, and exits 0. The
# guard test reads that list to check the leg's membership against the claim
# (mcp/pair_mailbox/test_verification_profile.py) — a leg that grew a
# hand-written list would be visible there.
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
    # A python file is a tracked .py module OR a tracked script whose shebang
    # says python. The shebang alone covered 65 of 141: the other 76 are
    # library modules, and a syntax error in any of them left this gate green
    # while it printed "65 file(s) parse" (round 127).
    is_py=0
    case "$f" in *.py) is_py=1 ;; esac
    case "$(head -1 "$f" 2>/dev/null || true)" in
        '#!'*python*) is_py=1 ;;
    esac
    [ "$is_py" -eq 1 ] || continue
    count=$((count + 1))
    if [ "$list_only" -eq 1 ]; then
        echo "$f"
        continue
    fi
    if ! python3 -c "import sys; compile(open(sys.argv[1], encoding='utf-8').read(), sys.argv[1], 'exec')" "$f" 2>/dev/null; then
        echo "SYNTAX FAIL: $f"
        python3 -c "compile(open('$f', encoding='utf-8').read(), '$f', 'exec')" 2>&1 | tail -3
        status=1
    fi
done

if [ "$list_only" -eq 1 ]; then
    exit 0
fi

if [ "$status" -eq 0 ]; then
    echo "python-syntax: $count file(s) parse"
else
    echo "python-syntax: FAILED ($count file(s) checked)" >&2
fi
exit "$status"
