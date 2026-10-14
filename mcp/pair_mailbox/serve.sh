#!/bin/sh
# Launch the pair-mailbox MCP server as a fixed role.
#
# Each agent gets its own entry in its MCP config pointing at this wrapper with a
# different role name. The role is baked into configuration rather than read from
# the environment, because an agent must not be able to claim a role it was not
# given: the whole write-authority boundary depends on the server knowing which
# process is which, and a value the process can set for itself is not that.
#
# Usage: serve.sh <role> [--state dir | --state=dir | state-dir]
set -eu

if [ "$#" -lt 1 ]; then
  echo "usage: serve.sh <role> [state-dir]" >&2
  exit 2
fi

role="$1"
shift
state=""
pane=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        --state) state="${2:-}"; shift 2;;
        --state=*) state="${1#--state=}"; shift;;
        --pane) pane="${2:-}"; shift 2;;
        --pane=*) pane="${1#--pane=}"; shift;;
        *) if [ -z "$state" ]; then case "$1" in --*) ;; *) state="$1";; esac; fi; shift;;
    esac
done
state="${state:-${PAIR_STATE:-}}"
if [ -z "$state" ]; then
    # The same walk-up as the plugin and extract_state: launching from a subdir
    # creates no second state. Shared configs carry no pin by design.
    # Outside a git repo no walk-up runs at all: or a probe from $HOME would
    # raise the server on junk ~/.pair stealing the role (measured 2026-10-07:
    # a --role opencode double on ~/.pair).
    top="$(git rev-parse --show-toplevel 2>/dev/null || true)"
    if [ -n "$top" ]; then
        dir="$(pwd)"
        for _ in 1 2 3 4 5 6 7 8 9 10 11 12; do
            if [ -f "$dir/.pair/state.json" ]; then state="$dir/.pair"; break; fi
            if [ "$dir" = "$top" ]; then break; fi
            parent="$(dirname -- "$dir")"
            if [ "$parent" = "$dir" ]; then break; fi
            dir="$parent"
        done
    fi
fi
# An empty state comes before any warnings: or `dirname -- ""` gives `.`, junk
# `state from ,` flying into the log. Refusing a stray state reads honest (a probe
# from $HOME must never raise the server on junk), but the message must tell "no
# git" from "no .pair" apart, saying what to do next (pairup).
if [ -z "$state" ]; then
    if git rev-parse --show-toplevel >/dev/null 2>&1; then
        echo "pair-mailbox: refusing: no .pair/state.json in $(pwd) or above (git root $(git rev-parse --show-toplevel 2>/dev/null)) — create it: cd <project> && pairup [--writer <role>] [--duo <cli>]" >&2
    else
        echo "pair-mailbox: refusing: no git repository here ($(pwd)) — refusing to create a stray state; cd into a project and run pairup first" >&2
    fi
    exit 4
fi
case "$state" in -*) echo "pair-mailbox: refusing flag-like state $state (a flag was taken for a path)" >&2; exit 2;; esac
# Visibility, not a ban: launching from a foreign dir is the main source of pane
# confusion across projects. Legit subdir launches never break.
# Stands PAST resolving, catching both walk-up and explicit --state/PAIR_STATE.
proj_root="$(dirname -- "$state")"; case "$(pwd)/" in "$proj_root"/*) ;; *) echo "pair-mailbox: NOTE: state from $state, but cwd $(pwd) — the pane will read foreign" >&2;; esac
if [ -n "${PAIR_ROLE:-}" ] && [ "${PAIR_ROLE}" != "$role" ]; then
  echo "pair-mailbox: refusing: PAIR_ROLE=${PAIR_ROLE} != argv role $role (role comes from config, never from env)" >&2
  exit 3
fi

here="$(cd -- "$(dirname -- "$0")" && pwd)"

# Prefer python3 explicitly: some agents run with a minimal PATH that finds
# `python` as Python 2, and this server needs 3.8 or newer.
python="$(command -v python3 || true)"
if [ -z "$python" ]; then
  echo "pair-mailbox: python3 not found on PATH" >&2
  exit 127
fi
# Version checking BEFORE launch: with no it the human on old Python takes 200+
# import errors instead of one version line. Exactly how install on macOS read,
# where system python3 is 3.9 while `str | None`-style annotations demand 3.10. The
# message must name what to do, not only what fails.
if ! "$python" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' 2>/dev/null; then
  echo "pair-mailbox: need python3 3.8 or newer, found $("$python" -c 'import platform; print(platform.python_version())' 2>/dev/null || echo '?')" >&2
  echo "pair-mailbox: install newer (brew install python), checking which python3 sits in PATH: command -v python3" >&2
  exit 6
fi

if [ -n "$pane" ]; then
  exec "$python" "$here/server.py" --role "$role" --state "$state" --pane "$pane"
else
  exec "$python" "$here/server.py" --role "$role" --state "$state"
fi