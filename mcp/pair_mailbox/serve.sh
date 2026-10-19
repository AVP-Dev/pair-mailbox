#!/bin/sh
# Launch the pair-mailbox MCP server as a fixed role.
#
# Each agent gets its own entry in its MCP config pointing at this wrapper with a
# different role name. The role comes from that config, and the environment may
# only DERIVE an instance of it ("agy" -> "agy-writer"), because an agent must
# not be able to claim a role it was not given: the whole write-authority
# boundary depends on the server knowing which process is which, and a value the
# process can set for itself is not that. A foreign PAIR_ROLE is refused; only
# an instance of the configured role is accepted.
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
_state_from_argv=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        --state) state="${2:-}"; _state_from_argv=1; shift 2;;
        --state=*) state="${1#--state=}"; _state_from_argv=1; shift;;
        --pane) pane="${2:-}"; shift 2;;
        --pane=*) pane="${1#--pane=}"; shift;;
        *) if [ -z "$state" ]; then case "$1" in --*) ;; *) state="$1"; _state_from_argv=1;; esac; fi; shift;;
    esac
done
state="${state:-${PAIR_STATE:-}}"
# Round 121: an env-sourced PAIR_STATE is NOT self-authoritative, and the check
# MUST run before the walk-up, never after it.
#
# Measured live in the nyx room: the operator opened `tmux new -s nyx`, ran
# `pairup`, and the "pair" served the foreign galatour room from both panes
# (`ps`: --state /mnt/storage/work/galatour/.pair from nyx:0.0 and nyx:0.1),
# while /mnt/storage/work/nyx-system/.pair was served by nobody. The NOTE that
# used to sit below only NAMED the mismatch and the server went on serving the
# foreign room: the guard that protects the room never triggered from env.
#
# Rule: argv `--state` stays explicit and trusted (an MCP config or an operator
# naming exactly which room). A PAIR_STATE from the ENVIRONMENT must name THIS
# project's own state dir — the toplevel of `dirname $state` must equal the
# toplevel of `pwd`. Anything else is dropped here so the walk-up below serves
# the project the server was launched in, not the room its shell remembered.
# Escape hatch is explicit: PAIR_TRUST_ENV=1, for tests and for an operator who
# knows exactly which room they want. Placing this after the walk-up is the bug
# it replaces — measured: the env value was already proven and the walk-up never
# ran again, so the mismatch survived to the answer.
if [ -n "$state" ] && [ -z "${_state_from_argv:-}" ] && [ "${PAIR_TRUST_ENV:-}" != "1" ]; then
    _state_top="$(cd -- "$(dirname -- "$state")" 2>/dev/null && git rev-parse --show-toplevel 2>/dev/null || true)"
    _cwd_top="$(git rev-parse --show-toplevel 2>/dev/null || true)"
    if [ -n "$_state_top" ] && [ -n "$_cwd_top" ] && [ "$_state_top" != "$_cwd_top" ]; then
        echo "pair-mailbox: ignoring PAIR_STATE=$state: it belongs to project $_state_top," >&2
        echo "pair-mailbox: but this server was launched from $_cwd_top. Serving this project's own .pair." >&2
        echo "pair-mailbox: (to serve the other room on purpose: PAIR_TRUST_ENV=1, or pass --state explicitly)" >&2
        state=""
    fi
fi
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
if [ -z "$state" ]; then
    if git rev-parse --show-toplevel >/dev/null 2>&1; then
        echo "pair-mailbox: refusing: no .pair/state.json in $(pwd) or above (git root $(git rev-parse --show-toplevel 2>/dev/null)) — create it: cd <project> && pairup [--writer <role>]   (two copies of one CLI: bin/pair-duo <cli>)" >&2
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

# Round 108 slice 1: the origin guard. That NOTE used to be all we had, and a
# scratch clone took it at its word — measured: a config left pointing at
# /tmp/wire_test would have served a role from a foreign tree on the next restart,
# the role lock free at that moment, so nothing refused. A record naming
# something other than what is there is a defect (round 104's class), so the
# tree the CODE came from must be the project the STATE belongs to.
#
# The identity is `git rev-parse --show-toplevel`, NOT the path string: the same
# tree is reachable through bind mounts and symlinks (/home/alexios/work/umputum
# and /mnt/storage/work/umputum are one repo), and a path comparison would have
# refused the live pair. The toplevel is canonical for both, and a scratch clone
# is a DIFFERENT toplevel — which is exactly the distinction we need.
# Walk up from the script's own directory until git answers, so the check works
# for a script copied into a repo root as well as one in its canonical
# mcp/pair_mailbox/ place (measured: a fixed ../.. found nothing when the script
# sits at the root, and a silent empty identity would have meant fail-open).
_code_top=""
_d="$(cd -- "$(dirname -- "$0")" && pwd -P)"
while [ -n "$_d" ] && [ "$_d" != "/" ]; do
  # `|| true` is not decoration: under `set -e` a failing command substitution
  # in an assignment EXITS the shell, and `2>/dev/null` hides only the message,
  # never the exit status (peer's find, seq 1625, measured: rc=128 from a
  # non-git --state, silently, before the handshake). With it, an unanswered
  # git rev-parse yields "" — which is what fail-open means.
  _top="$(cd -- "$_d" 2>/dev/null && git rev-parse --show-toplevel 2>/dev/null || true)"
  if [ -n "$_top" ]; then _code_top="$_top"; break; fi
  _d="$(dirname -- "$_d")"
done
_state_top="$(cd -- "$proj_root" 2>/dev/null && git rev-parse --show-toplevel 2>/dev/null || true)"
if [ -n "$_code_top" ] && [ -n "$_state_top" ] && [ "$_code_top" != "$_state_top" ]; then
  # Two projects, one server code. The multi-project model: the autonomy repo
  # (pair-mailbox itself, the engine) is install.sh's ROOT, and the target
  # project is wherever pairup raised .pair. The engine is NOT copied into every
  # target — bin/pairup is the shim (~/.local/bin/pairup), state resolves by
  # walk-up from cwd, and the MCP entry points at the engine's serve.sh for
  # every project. So code-from-elsewhere is the NORMAL case: a galatour pane
  # runs /mnt/storage/work/umputum/mcp/pair_mailbox/serve.sh with galatour state.
  # Assuming the code must live inside the state's repo broke that (round 108's
  # guard: e4e0550 wrote a remedy pointing at $_state_top/mcp/pair_mailbox/serve.sh
  # — a path that does not exist in a target project).
  #
  # What the guard must still catch: a scratch clone taking a role. So the
  # foreign code root is allowed ONLY when the state's project has sanctioned
  # it AND carries no package of its own AND the code is not in scratch space.
  _state_own_pkg="$_state_top/mcp/pair_mailbox/serve.sh"
  # The sanction lives in the STATE dir itself, not derived by assuming the
  # state dir is called .pair: `--state custom_dir` is a legal invocation
  # (the installer's verify path, bin/pairdoctor), and a hardcodedname would
  # have looked for the record in a directory that was never used (critic's
  # find, round 120). $state is already resolved and absolute at this point.
  _engine_file="$state/engine"
  _engine_root=""
  [ -f "$_engine_file" ] && _engine_root="$(cat -- "$_engine_file" 2>/dev/null || true)"
  # The sanction's identity is the engine's git-toplevel, never the path string:
  # one engine, several reachable paths (bind mounts, symlinks) — same check as
  # above, and a string comparison would refuse the live pair. Written by
  # bin/pairup, refreshed when the engine moves.
  _engine_top=""
  if [ -n "$_engine_root" ] && [ -d "$_engine_root/mcp/pair_mailbox" ]; then
    _et="$(cd -- "$_engine_root" 2>/dev/null && git rev-parse --show-toplevel 2>/dev/null || true)"
    if [ -n "$_et" ] && [ -d "$_et/mcp/pair_mailbox" ]; then _engine_top="$_et"; fi
  fi
  _scratch=""
  case "$_code_top" in
    /tmp|/tmp/*|/var/tmp|/var/tmp/*|/dev/shm|/dev/shm/*) _scratch="$_code_top";;
  esac
  # Round 108's scar, unconditional: the measured defect was a config left
  # pointing at /tmp/wire_test, and a scratch root can NEVER serve a role —
  # no sanction, project layout or plea changes that. It stands first, so a
  # forged sanction in a scratch clone buys nothing.
  if [ -n "$_scratch" ]; then
    echo "pair-mailbox: REFUSING: the server code lives in a scratch tree." >&2
    echo "pair-mailbox:   code repo:  $_code_top (scratch space)" >&2
    echo "pair-mailbox:   state repo: $_state_top" >&2
    echo "pair-mailbox: a leftover from an installer test served this role from stale" >&2
    echo "pair-mailbox: code while everything looked correct (round 108, measured)." >&2
    echo "pair-mailbox: fix the MCP entry to the real engine root recorded in" >&2
    echo "pair-mailbox:   $proj_root/.pair/engine" >&2
    exit 5
  fi
  if [ -f "$_state_own_pkg" ]; then
    # The project HAS its own engine: then the entry must point at it, and a
    # foreign root here is exactly the scratch-clone case round 108 caught.
    echo "pair-mailbox: REFUSING: the server code lives in a foreign tree." >&2
    echo "pair-mailbox:   code repo:  $_code_top" >&2
    echo "pair-mailbox:   state repo: $_state_top (own mcp/pair_mailbox/ found)" >&2
    echo "pair-mailbox: this project carries its own pair-mailbox package, so the" >&2
    echo "pair-mailbox: MCP entry must point at that one:" >&2
    echo "pair-mailbox:   $_state_top/mcp/pair_mailbox/serve.sh" >&2
    echo "pair-mailbox: (or rerun ./install.sh in the correct tree)." >&2
    exit 5
  fi
  if [ -z "$_engine_top" ] || [ "$_engine_top" != "$_code_top" ]; then
    echo "pair-mailbox: REFUSING: the server code lives in a foreign tree." >&2
    echo "pair-mailbox:   code repo:  $_code_top" >&2
    echo "pair-mailbox:   state repo: $_state_top" >&2
    echo "pair-mailbox: the state's project ($_state_top) has no own pair-mailbox" >&2
    echo "pair-mailbox: package, so it runs an engine from another tree — but that" >&2
    echo "pair-mailbox: engine is not sanctioned here." >&2
    if [ -z "$_engine_root" ]; then
      echo "pair-mailbox:   no engine sanction in $proj_root/.pair/engine" >&2
      echo "pair-mailbox: fix it: cd $_state_top && <engine-root>/bin/pairup" >&2
    else
      echo "pair-mailbox:   sanctioned engine: $_engine_root" >&2
      echo "pair-mailbox: fix it: cd $_state_top && bin/pairup  (refreshes the sanction from this $(basename -- "$_code_top") engine), or" >&2
      echo "pair-mailbox:        change the MCP entry to the sanctioned engine's serve.sh:" >&2
      echo "pair-mailbox:        $_engine_root/mcp/pair_mailbox/serve.sh" >&2
    fi
    exit 5
  fi
  echo "pair-mailbox: NOTE: serving state from $_state_top with engine code from $_code_top (sanctioned)" >&2
fi
if [ -n "${PAIR_ROLE:-}" ] && [ "${PAIR_ROLE}" != "$role" ]; then
  # The role may be DERIVED from the configured one, never REPLACED by it.
  #
  # Duo mode needs two instances of ONE CLI in a project, and the MCP config is
  # per CLI: both instances read the same file and would claim the same role.
  # The instance suffix says WHICH instance ("agy" -> "agy-writer"), and it may
  # only extend the role the config gave. A FOREIGN role stays a refusal: the
  # measured 2026-10-07 case was a process picking up a role it was not given
  # (PAIR_ROLE=opencode served by an omp server), and the role decides who may
  # write and whose verdict counts.
  case "$PAIR_ROLE" in
    "${role}"-*) ;;
    "${role}"_*) ;;
    *) echo "pair-mailbox: refusing: PAIR_ROLE=${PAIR_ROLE} is neither the configured role ${role} nor an instance of it" >&2
       exit 3;;
  esac
  # The role names a mailbox directory and a wake file, so the derived name may
  # only carry characters that cannot climb out of .pair/: "agy-writer" and
  # "agy_2" pass, "agy-../.." does not.
  case "$PAIR_ROLE" in
    *[!A-Za-z0-9_-]*)
      echo "pair-mailbox: refusing: PAIR_ROLE=${PAIR_ROLE} carries characters a role may not hold" >&2
      exit 3;;
  esac
  role="$PAIR_ROLE"
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