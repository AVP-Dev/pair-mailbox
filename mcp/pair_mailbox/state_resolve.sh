#!/bin/sh
# Which room is mine? One resolver, so every entry point answers alike.
#
# Round 121's ruling: "the project root owns the state, never the inherited
# environment". It was implemented in two places and contradicted in three, and
# the cost was never the bugs — it was that the rule had no single place, so the
# next entry point would get it wrong the same way.
#
# This is the SHELL face of `mcp/pair_mailbox/state_resolve.py`: the same rule,
# the same wording, one implementation per language. The shell scripts call
# this file; the Python ones import the module.
#
# Usage: state_resolve.sh [--from-argv] [explicit-state]
#   --from-argv     the explicit state came from argv (a flag or a positional
#                   argument), not from the environment: trusted, always
#   explicit-state  the room the operator named on the command line
#
# Prints two lines: the room, then the reason. The reason is what a refusal is
# made of: a refusal that does not say WHICH room was refused and WHICH was
# used instead is the same silence, one step later.
#
# The environment values are passed as ARGUMENTS by the caller, not inherited:
# a shell variable that was never exported is still a value the operator set.
set -eu

_from_argv=0
_explicit=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        --from-argv) _from_argv=1; shift ;;
        *) _explicit="$1"; shift ;;
    esac
done

_here="$(cd -- "$(dirname -- "$0")" && pwd -P)"
# This file lives in <root>/mcp/pair_mailbox/, so the root is two levels up.
_root="$(cd -- "$_here/../.." && pwd -P)"

python3 - "$_root" "$_explicit" "$_from_argv" "${PAIR_STATE:-}" "${PAIR_TRUST_ENV:-}" <<'PY'
import sys
from pathlib import Path

root = Path(sys.argv[1])
explicit = sys.argv[2] or None
from_argv = sys.argv[3] == "1"
env = {"PAIR_STATE": sys.argv[4], "PAIR_TRUST_ENV": sys.argv[5]}

sys.path.insert(0, str(root / "mcp" / "pair_mailbox"))
from state_resolve import resolve_state_dir

# An explicit --state is the operator or the MCP config speaking: trusted.
# Everything else is the environment's, and the resolver judges it.
room, reason = resolve_state_dir(explicit=explicit if from_argv else None, env=env)

# The server's own wording for the environment case: it serves this project's
# own room, and says which one it dropped and which project it is serving.
if not from_argv and env.get("PAIR_STATE"):
    wanted = Path(env["PAIR_STATE"]).expanduser()
    try:
        wanted = wanted.resolve()
    except OSError:
        wanted = None
    if wanted is not None and wanted != room.resolve():
        reason = (f"ignoring PAIR_STATE={env['PAIR_STATE']}: it belongs to another "
                  f"project; this server was launched from {Path.cwd()}. "
                  f"Serving this project's own {room} instead "
                  "(to serve the other room on purpose: PAIR_TRUST_ENV=1, or pass "
                  "--state explicitly)")

print(room)
print(reason)
PY
