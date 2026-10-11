"""`pairctl`: CLI человека поверх состояния пары. Точка входа и разбор аргументов.

Вынесено из `server.py` отдельной ответственностью: здесь только грамматика
команд и маршрутизация, а сами команды живут в трёх соседних модулях по
владению состоянием:

- `pairctl_authority.py` — меняет раунд: кто пишет, режим, автономия, чекпоинт,
  леджер задач, сброс;
- `pairctl_panes.py` — адреса панелей tmux;
- `pairctl_read.py` — только чтение: дайджест, ящик, журнал, заметки, обзор
  проектов (последнее оттуда же импортирует `bin/pairchat`).

Почему не один модуль `pairctl_cli.py` на 725 строк, как это было в `server.py`:
упаковка была не разбиением по границам, а переносом монолита в новое место.
Границы здесь проходят по тому, кто владеет состоянием, и по ним видно, что
изменение попадёт в журнал: команда из `pairctl_read.py` не может ничего
испортить, потому что записи в состояние в ней нет вовсе.

`server.py` остаётся точкой входа MCP-сервера, и `--state`/`--role` разбираются
в нём же: менять имя или путь файла нельзя, от них зависят `serve.sh`,
`install.sh` и конфигурация MCP у всех четырёх ролей.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Callable

from server import (
    AUTONOMIES,
    DIGEST_TOKEN_BUDGET,
    LOG_DEFAULT_LIMIT,
    MODES,
    Store,
)

# Импорты соседних модулей ленивые, и это не оптимизация. Модули `pairctl_*`
# импортируют `server`, поэтому на верхнем уровне возник бы цикл: `server`
# зовёт `main`, а `main` зовёт сюда. Внутри функции цикл не образуется, потому
# что к этому моменту `server` уже загружен.
Command = Callable[[Store, argparse.Namespace], int]


def _commands() -> dict[str, str]:
    """Имя команды -> модуль, который её реализует.

    Словарь имён, а не импорты: сам импорт делается в `run_ctl`. Так каждая
    команда остаётся в своём файле, а этот не знает про их содержимое ничего,
    кроме того, что модуль с таким именем существует.
    """
    return {
        "status": "pairctl_authority",
        "assign": "pairctl_authority",
        "roles": "pairctl_authority",
        "release": "pairctl_authority",
        "mode": "pairctl_authority",
        "autonomy": "pairctl_authority",
        "launch-autonomy": "pairctl_authority",
        "ok": "pairctl_authority",
        "task": "pairctl_authority",
        "tasks": "pairctl_authority",
        "reset": "pairctl_authority",
        "keys": "pairctl_authority",
        "pane": "pairctl_panes",
        "roster": "pairctl_panes",
        "panes": "pairctl_panes",
        "digest": "pairctl_read",
        "why": "pairctl_read",
        "inbox": "pairctl_read",
        "log": "pairctl_read",
        "all": "pairctl_read",
        "notes": "pairctl_read",
        "budget": "budget",
        "worktree": "worktree",
        "pr": "review_gate",
    }

def run_ctl(args: argparse.Namespace, state_dir: Path) -> int:
    """Разобрать команду и позвать её реализацию.

    Порядок здесь намеренный: `Store` создаётся ДО отказа по неизвестной
    команде. Раньше неизвестная команда тоже создавала store, но это было
    незаметно — состояние не писалось. Сейчас проверка идёт первой, чтобы
    опечатка в имени команды не выглядела как «сервер что-то сделал».
    """
    command = args.ctl_command
    module_name = _commands().get(command or "")
    if not module_name:
        print(f"unknown command {command!r}", file=sys.stderr)
        return 2
    module = __import__(module_name)
    handler: Command = getattr(module, "cmd_" + command.replace("-", "_"))
    return handler(Store(state_dir), args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pair-mailbox",
        description="MCP server and control tool for a two-agent collaboration.",
    )
    parser.add_argument("--role", help="This agent's role name. Fixed at configuration time.")

    sub = parser.add_subparsers(dest="ctl_command")
    sub.add_parser("status", help="show shared state")

    assign = sub.add_parser("assign", help="human: give write authority to a role")
    assign.add_argument("role")

    sub.add_parser("release", help="human: clear write authority")

    digest = sub.add_parser(
        "digest",
        help="human: the same bounded slice the agents get, in under 500 tokens",
    )
    digest.add_argument("--budget", type=int, default=DIGEST_TOKEN_BUDGET,
                        help=f"token ceiling for the whole answer, {DIGEST_TOKEN_BUDGET} by default")
    digest.add_argument("--role", default="",
                        help="say 'you are the writer' when this is this role, saves a lookup")

    pane = sub.add_parser(
        "pane",
        help="human: bind a role to a pane by hand, or --clear to go back to what tmux says",
    )
    pane.add_argument("--role", required=False, default="",
                      help="role whose binding to set (или позиционно: pane <role> [address])")
    pane.add_argument("positional", nargs="*",
                      help="позиционная форма из карточки: <role> [address]; "
                           "address: номер панели или <session>:<window>.<pane>; "
                           "без address — только показ, без записи")
    pane.add_argument("--current", action="store_true",
                      help="привязать роль к $TMUX_PANE текущей панели; без флага "
                           "pane <role> ничего не меняет, только показывает")
    pane.add_argument("--clear", action="store_true",
                      help="drop the override; the pane goes back to what the process reports")

    roster = sub.add_parser("roster", help="human: show who is registered, --prune to clean")
    roster.add_argument("--prune", action="store_true",
                        help="remove roles whose process is dead AND whose pane is gone")

    sub.add_parser("panes", help="human: live tmux panes mapped to project, role and liveness")

    roles = sub.add_parser(
        "roles",
        help="human: pin who always writes and who always reviews, instead of racing each round",
    )
    roles.add_argument("--writer", help="role that always holds write authority")
    roles.add_argument("--critic", help="role that always reviews")
    roles.add_argument("--swap", action="store_true", help="exchange writer and critic")
    roles.add_argument("--clear", action="store_true", help="drop the pinning, go back to the race")
    roles.add_argument("--global", dest="global_", action="store_true",
                       help="also remember it for every project (PAIR_ROLES, default ~/.config/pair-mailbox/roles.json)")

    round_mode = sub.add_parser(
        "mode",
        help="human: choose what a round is for — 'fix' changes code, 'audit' does not",
    )
    round_mode.add_argument("mode", choices=list(MODES))

    autonomy = sub.add_parser(
        "autonomy",
        help="human: how far the pair may go without you — 'observe', 'semi' or 'auto'",
    )
    autonomy.add_argument("level", choices=list(AUTONOMIES))

    launch = sub.add_parser(
        "launch-autonomy",
        help="choose the level once at launch, from a flag, a task line, .pair/autonomy, "
             "or the default 'auto' — strongest source first",
    )
    launch.add_argument("--flag", default=None, choices=list(AUTONOMIES),
                        help="what bin/pair-duo was given")
    launch.add_argument("--task", default="", help="the task text, may start with 'autonomy: semi'")

    ok = sub.add_parser(
        "ok",
        help="human: say yes at the verify checkpoint, so the pair may close the round",
    )
    ok.add_argument("--revoke", action="store_true",
                    help="human: take the yes back, e.g. after looking at what changed since")

    task = sub.add_parser("task", help="human: add a task to the shared list")
    task.add_argument("text", nargs="?")
    task.add_argument("--drop", metavar="ID", default=None,
                      help="close task ID as dropped, with the reason below")
    task.add_argument("--done", metavar="ID", default=None,
                      help="close task ID as done")
    task.add_argument("--reason", default="",
                      help="why a task is dropped; required, an empty reason is refused")

    sub.add_parser("tasks", help="human: print the shared task list with statuses")

    inbox = sub.add_parser("inbox", help="show a role's pending messages")
    inbox.add_argument("role")

    log = sub.add_parser("log", help="print the audit trail")
    log.add_argument("--limit", type=int, default=LOG_DEFAULT_LIMIT,
                     help=f"how many of the newest entries to print, {LOG_DEFAULT_LIMIT} by default")
    log.add_argument("--all", action="store_true",
                     help="print the whole active file; rotated-away entries live "
                          "in .pair/transcript-archive/ and are not included")

    sub.add_parser("notes", help="print shared notes")

    everywhere = sub.add_parser(
        "all", help="one row per project, across every project with a .pair"
    )
    everywhere.add_argument("paths", nargs="*", help="extra project or .pair directories")
    everywhere.add_argument("--stuck-min", type=int, default=30)
    everywhere.add_argument("--json", action="store_true")

    why = sub.add_parser("why", help="who writes, and the reason they gave")
    why.add_argument("paths", nargs="*", help="extra project or .pair directories")
    why.add_argument("--json", action="store_true")

    reset = sub.add_parser("reset", help="wipe shared state")
    reset.add_argument("--yes", action="store_true")

    keys = sub.add_parser("keys", help="human: Ed25519-ключи ролей (gen)")
    keys.add_argument("keys_command", nargs="?", help="'gen'")
    keys.add_argument("role", nargs="?", default="", help="роль для gen")

    budget_p = sub.add_parser("budget", help="human: лимиты и метрики раунда (бюджет)")
    budget_p.add_argument("--reset", action="store_true", help="сбросить счетчики бюджета текущего раунда")
    budget_p.add_argument("--json", action="store_true", help="вывести метрики в json")

    wt_p = sub.add_parser("worktree", help="human: управление git worktrees для параллельных сессий")
    wt_p.add_argument("worktree_action", nargs="?", choices=["list", "add", "remove", "prune"], default="list")
    wt_p.add_argument("branch", nargs="?", help="имя ветки (для add)")
    wt_p.add_argument("--target", help="путь или ветка для remove")
    wt_p.add_argument("--force", action="store_true", help="принудительное удаление (для remove)")
    wt_p.add_argument("--json", action="store_true", help="вывести в формате json")

    pr_p = sub.add_parser("pr", help="human/writer: машинный гейт слияния PR (merge)")
    pr_p.add_argument("pr_action", nargs="?", choices=["merge"], default="merge")
    pr_p.add_argument("pr_id", nargs="?", help="PR number or branch name")
    pr_p.add_argument("--force", action="store_true", help="обойти проверки (требует --reason)")
    pr_p.add_argument("--reason", default="", help="причина принудительного слияния при --force")

    return parser


def ctl_main(argv: list[str], store: Store | None = None) -> int:
    """Выполнить команду `pairctl` по готовому argv.

    `store` передаётся только в тестах, которые правят состояние напрямую. В
    боевом запуске его нет, и `Store` строится здесь: одна точка создания
    означает, что «каталог состояния выбран» происходит ровно в одном месте.
    """
    from pairctl_read import extract_state

    state_dir, rest = extract_state(argv)
    args = build_parser().parse_args(rest)
    if not args.ctl_command:
        build_parser().print_help()
        return 0
    if store is not None:
        command = args.ctl_command
        module = __import__(_commands()[command])
        return getattr(module, "cmd_" + command.replace("-", "_"))(store, args)
    return run_ctl(args, state_dir)