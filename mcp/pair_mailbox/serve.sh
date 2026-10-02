#!/bin/sh
# Launch the pair-mailbox MCP server as a fixed role.
#
# Each agent gets its own entry in its MCP config pointing at this wrapper with a
# different role name. The role is baked into configuration rather than read from
# the environment, because an agent must not be able to claim a role it was not
# given: the whole write-authority boundary depends on the server knowing which
# process is which, and a value the process can set for itself is not that.
#
# Usage: serve.sh <role> [state-dir]
set -eu

if [ "$#" -lt 1 ]; then
  echo "usage: serve.sh <role> [state-dir]" >&2
  exit 2
fi

role="$1"
state="${2:-.pair}"

here="$(cd -- "$(dirname -- "$0")" && pwd)"

# Prefer python3 explicitly: some agents run with a minimal PATH that finds
# `python` as Python 2, and this server needs 3.10+ for match-statement syntax.
python="$(command -v python3 || true)"
if [ -z "$python" ]; then
  echo "pair-mailbox: python3 not found on PATH" >&2
  exit 127
fi

exec "$python" "$here/server.py" --role "$role" --state "$state"