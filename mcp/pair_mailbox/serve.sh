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
while [ "$#" -gt 0 ]; do
    case "$1" in
        --state) state="${2:-}"; shift 2;;
        --state=*) state="${1#--state=}"; shift;;
        *) if [ -z "$state" ]; then case "$1" in --*) ;; *) state="$1";; esac; fi; shift;;
    esac
done
state="${state:-${PAIR_STATE:-}}"
if [ -z "$state" ]; then
    # Тот же walk-up, что у плагина и extract_state: запуск из подкаталога
    # не создаёт второе состояние. Общие конфиги pin не несут by design.
    dir="$(pwd)"
    for _ in 1 2 3 4 5 6 7 8 9 10 11 12; do
        if [ -f "$dir/.pair/state.json" ]; then state="$dir/.pair"; break; fi
        parent="$(dirname -- "$dir")"
        if [ "$parent" = "$dir" ]; then break; fi
        dir="$parent"
    done
    state="${state:-.pair}"
fi
if [ -n "${PAIR_ROLE:-}" ] && [ "${PAIR_ROLE}" != "$role" ]; then
  echo "pair-mailbox: refusing: PAIR_ROLE=${PAIR_ROLE} != argv role $role (role comes from config, never from env)" >&2
  exit 3
fi

here="$(cd -- "$(dirname -- "$0")" && pwd)"

# Prefer python3 explicitly: some agents run with a minimal PATH that finds
# `python` as Python 2, and this server needs 3.10+ for match-statement syntax.
python="$(command -v python3 || true)"
if [ -z "$python" ]; then
  echo "pair-mailbox: python3 not found on PATH" >&2
  exit 127
fi

exec "$python" "$here/server.py" --role "$role" --state "$state"