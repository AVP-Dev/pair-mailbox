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
    # Тот же walk-up, что у плагина и extract_state: запуск из подкаталога
    # не создаёт второе состояние. Общие конфиги pin не несут by design.
    # Вне git-репозитория walk-up не идёт вовсе: иначе probe из $HOME
    # поднимает сервер на мусорном ~/.pair и угоняет роль
    # (измерено 2026-10-07: дубль --role opencode на ~/.pair).
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
# Пустой state — до любых предупреждений: иначе `dirname -- ""` даёт `.`
# и в лог летит мусор `state из ,`. Отказ создавать stray state — честный
# (probe из $HOME не должен поднимать сервер на мусоре), но сообщение обязано
# различать "нет git" и "нет .pair" и говорить что делать дальше (pairup).
if [ -z "$state" ]; then
    if git rev-parse --show-toplevel >/dev/null 2>&1; then
        echo "pair-mailbox: refusing: no .pair/state.json in $(pwd) or above (git root $(git rev-parse --show-toplevel 2>/dev/null)) — create it: cd <project> && pairup [--writer <role>] [--duo <cli>]" >&2
    else
        echo "pair-mailbox: refusing: no git repository here ($(pwd)) — refusing to create a stray state; cd into a project and run pairup first" >&2
    fi
    exit 4
fi
case "$state" in -*) echo "pair-mailbox: refusing flag-like state $state (a flag was taken for a path)" >&2; exit 2;; esac
# Видимость, не запрет: запуск из чужой папки — главный источник путаницы
# панелей между проектами. Легитимные запуски из подкаталогов не ломаем.
# Стоит ПОСЛЕ резолва, чтобы ловить и walk-up, и явный --state/PAIR_STATE.
proj_root="$(dirname -- "$state")"; case "$(pwd)/" in "$proj_root"/*) ;; *) echo "pair-mailbox: внимание: state из $state, а cwd $(pwd) — панель будет выглядеть чужой" >&2;; esac
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
# Проверка версии ДО запуска: без неё человек на старом Python получает
# 200+ ошибок импорта вместо одной строки о версии. Именно так выглядела
# установка на macOS, где системный python3 — 3.9, а аннотации вида `str | None`
# требуют 3.10. Сообщение обязано называть, что делать, а не только что не так.
if ! "$python" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' 2>/dev/null; then
  echo "pair-mailbox: нужен python3 3.8 или новее, а найден $("$python" -c 'import platform; print(platform.python_version())' 2>/dev/null || echo '?')" >&2
  echo "pair-mailbox: поставь новее (brew install python) и проверь, какой python3 в PATH: command -v python3" >&2
  exit 6
fi

if [ -n "$pane" ]; then
  exec "$python" "$here/server.py" --role "$role" --state "$state" --pane "$pane"
else
  exec "$python" "$here/server.py" --role "$role" --state "$state"
fi