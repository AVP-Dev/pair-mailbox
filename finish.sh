#!/usr/bin/env python3
"""Одна команда: поставить, проверить, поднять пару и сверхвизора.

Порядок здесь не произвольный. Каждый шаг зависит от предыдущего, и пропуск
любого даёт ровно ту поломку, которую эта система чинит:

  1. тесты до установки. Установщик трогает ваши конфиги; если в репозитории
     красный тест, сначала чиним код, потом трогаем машину.
  2. установка. Кладёт mcp.pair в конфиги, навыки и блок парной работы.
  3. сверхвизор сначала, агенты потом. Иначе первые минуты пары он не видит.
  4. агенты в свежих окнах tmux. Старый процесс держит в памяти прежний код:
     правка файла на диске не действует, пока процесс жив.
  5. сверхвизор перечитывает состояние и проверяет, что у каждой роли есть
     живая панель. Нет панели — доставка невозможна, и это надо сказать сразу.

Запуск:

    ./finish.sh                 # всё
    ./finish.sh --no-pair       # поставить и проверить, агентов не поднимать
    ./finish.sh --task "текст"  # поднять сразу с задачей
"""
import argparse
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

SHELL = os.environ.get("SHELL") or "/bin/sh"

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "mcp" / "pair_mailbox"))


def run(argv, **kw):
    print(f"\n$ {' '.join(str(a) for a in argv)}", flush=True)
    return subprocess.run([str(a) for a in argv], cwd=str(ROOT), **kw)


def step(title: str) -> None:
    print(f"\n=== {title} " + "=" * max(0, 60 - len(title)), flush=True)


def python_ok(argv: list[str]) -> bool:
    result = run([sys.executable, *argv])
    return result.returncode == 0


def launch_command_for(role: str) -> str | None:
    """Команда запуска роли из cli-profiles.json.

    Роль и имя CLI совпадают (opencode, agy, claude, codex), поэтому профиль
    ищем по имени роли. Неизвестная роль — это не повод падать: показываем, что
    не нашли, и оставляем человека разбираться.
    """
    try:
        import json as _json
        profiles = _json.loads((ROOT / "cli-profiles.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    entry = profiles.get(role)
    if isinstance(entry, dict):
        cmd = entry.get("launch")
        if cmd:
            return str(cmd)
    return None


def launch_standing_pair(args) -> None:
    """Постоянная расстановка: писатель и критик из policy, каждый своим CLI.

    Право записи здесь не трогаем. Оно уже у того, кого назначил человек, и
    скрипт, который его двигает, нарушает PROTOCOL.md §2 — раньше именно это и
    происходило, молча, при каждом запуске.
    """
    import json as _json

    try:
        state = _json.loads((ROOT / args.state / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print("не прочиталось состояние — кого поднимать, неизвестно", file=sys.stderr)
        return
    policy = state.get("policy") if isinstance(state.get("policy"), dict) else {}
    writer_role = policy.get("writer") or state.get("writer")
    critic_role = policy.get("critic")
    if not critic_role:
        known = [r for r in (state.get("roles") or {}) if r != writer_role]
        critic_role = known[0] if known else None
    if not writer_role:
        print("в расстановке нет писателя: `bin/pairctl roles --writer <роль>`", file=sys.stderr)
        return
    if not critic_role:
        print("в расстановке нет критика: `bin/pairctl roles --critic <роль>`", file=sys.stderr)
        return
    if writer_role == critic_role:
        print("писатель и критик одна и та же роль — так пара не работает", file=sys.stderr)
        return

    missing = [r for r in (writer_role, critic_role) if not launch_command_for(r)]
    if missing:
        print(f"в cli-profiles.json нет команды запуска для: {', '.join(missing)}", file=sys.stderr)
        return

    state_path = (ROOT / args.state).resolve()
    init = lambda role, label: (
        f"export PAIR_ROLE='{role}' PAIR_STATE='{state_path}'; cd '{ROOT}'; "
        f"echo '=== {label}: {role} ==='; {launch_command_for(role)}"
    )
    session = f"pair-{writer_role}-{critic_role}-{int(time.time())}"
    writer_init = init(writer_role, "PAIR WRITER")
    critic_init = init(critic_role, "PAIR CRITIC")

    if os.environ.get("TMUX"):
        run(["tmux", "split-window", "-h", "-c", str(ROOT), f'{shlex.quote(SHELL)} -c '
             f'{shlex.quote(critic_init)}'])
        run(["tmux", "select-pane", "-L"])
    else:
        run(["tmux", "new-session", "-d", "-s", session, "-n", "pair", "-c", str(ROOT),
             f'{shlex.quote(SHELL)} -c {shlex.quote(writer_init)}'])
        run(["tmux", "split-window", "-t", session, "-h", "-c", str(ROOT),
             f'{shlex.quote(SHELL)} -c {shlex.quote(critic_init)}'])
        run(["tmux", "select-layout", "-t", session, "even-horizontal"])
        run(["tmux", "attach-session", "-t", session])
    print(f"постоянная расстановка: писатель {writer_role}, критик {critic_role}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Поднять парную сессию целиком")
    parser.add_argument("--task", default="", help="задача для пары")
    parser.add_argument("--cli", default="opencode",
                        help="CLI для обоих окон; используется только с --duo")
    parser.add_argument("--duo", action="store_true",
                        help="поднять две копии одного CLI вместо постоянной расстановки. "
                             "Передаёт право записи на <cli>-writer, поэтому только по "
                             "явному выбору человека")
    parser.add_argument("--no-prune", action="store_true",
                        help="не чистить ростер от мёртвых записей")
    parser.add_argument("--no-pair", action="store_true",
                        help="поставить и проверить, но агентов не поднимать")
    parser.add_argument("--state", default=os.environ.get("PAIR_STATE", ".pair"))
    parser.add_argument("--autonomy", default="", choices=("", "observe", "semi", "auto"))
    parser.add_argument("--roles", default="", help="writer,critic через запятую")
    args = parser.parse_args()

    failed = []

    step("1. тесты до установки")
    for suite in ("mcp/pair_mailbox/test_protocol.py", "bin/pairpilot_test.py",
                  "install_test.py"):
        if not python_ok([suite]):
            failed.append(suite)
    if failed:
        print(f"\nКРАСНО: {', '.join(failed)}")
        print("Сначала почините тесты. Установщик трогает ваши конфиги, и при")
        print("красном коде он оставит машину в промежуточном состоянии.")
        return 1

    step("2. установка")
    install = run([sys.executable, ROOT / "install.sh"])
    if install.returncode != 0:
        failed.append("install.sh")

    step("3. постоянные роли")
    if args.roles and "," in args.roles:
        writer, critic = (part.strip() for part in args.roles.split(",", 1))
        run([sys.executable, ROOT / "mcp/pair_mailbox/server.py",
             "--state", args.state, "roles", "--writer", writer, "--critic", critic])
    elif args.roles:
        run([sys.executable, ROOT / "mcp/pair_mailbox/server.py",
             "--state", args.state, "roles", "--writer", args.roles.strip()])

    step("4. автономность")
    if args.autonomy:
        run([sys.executable, ROOT / "mcp/pair_mailbox/server.py",
             "--state", args.state, "autonomy", args.autonomy])
    else:
        print("не задана, остаётся текущая (fresh default auto)")

    if args.no_pair:
        print("\nГотово: установлено и проверено. Агенты не поднимались (--no-pair).")
        return 1 if failed else 0

    step("5. сверхвизор ДО агентов")
    if shutil.which("tmux") is None:
        print("tmux не найден. Живая доставка без него невозможна.")
        return 1
    pilot = subprocess.Popen([sys.executable, str(ROOT / "bin" / "pairpilot"),
                              "--state", args.state],
                             cwd=str(ROOT), stdout=open(ROOT / ".pair" / "pairpilot.log", "a"),
                             stderr=subprocess.STDOUT, start_new_session=True)
    print(f"сверхвизор: pid {pilot.pid}, лог .pair/pairpilot.log")

    step("6. агенты в свежих окнах")
    if args.task:
        run([ROOT / "bin" / "pairctl", "--state", args.state, "task", args.task])
    # Ростер чистим до запуска, иначе свежие агенты Registerятся поверх записей
    # прошлых сессий и получают чужие панели.
    if not args.no_prune:
        run([ROOT / "bin" / "pairctl", "--state", args.state, "roster", "--prune"])
    if args.duo:
        # Явный выбор человека. bin/pair-duo назначает писателем <cli>-writer, и
        # раньше это происходило молча при любом запуске — то есть скрипт двигал
        # право записи без спроса, а это запрещено PROTOCOL.md §2.
        print(f"duo-режим: две копии {args.cli}, писателем станет {args.cli}-writer")
        run([ROOT / "bin" / "pair-duo", args.cli] + ([args.task] if args.task else []))
    else:
        launch_standing_pair(args)

    step("7. что получилось")
    time.sleep(8)  # агентам нужно время на регистрацию в MCP
    from server import Store, is_pid_alive, pane_exists  # noqa: E402

    state = Store(ROOT / args.state).read()
    roles = state.get("roles") or {}
    writer = state.get("writer")
    print(f"задача:  {state.get('task') or 'НЕ ЗАДАЧЕНА'}")
    print(f"фаза:    {state.get('phase')}   режим: {state.get('mode')}"
          f"   автономность: {state.get('autonomy')}")
    print(f"писатель: {writer or 'НЕ НАЗНАЧЕН'}")
    print("\nроль          панель   pid     состояние")
    for name, info in sorted(roles.items()):
        pane = info.get("pane")
        pid = info.get("pid")
        alive = is_pid_alive(pid) if pid else None
        if not pane:
            note = "НЕТ ПАНЕЛИ — доставка невозможна"
        elif not pane_exists(pane):
            note = "панель мертва"
        elif alive is False:
            note = "процесс мёртв (супервизор перезапустит)"
        else:
            note = "жив"
        print(f"{name:12} {str(pane):8} {str(pid):7} {note}")

    print(f"\nсверхвизор: {'работает, pid ' + str(pilot.pid) if pilot.poll() is None else 'УМЕР'}")
    print("переписка:  bin/pairchat")
    print("остановить: kill " + str(pilot.pid))

    if failed:
        print(f"\nНЕ ПРОШЛО: {', '.join(failed)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())