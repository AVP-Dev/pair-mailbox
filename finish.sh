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
import shutil
import subprocess
import sys
import time
from pathlib import Path

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


def main() -> int:
    parser = argparse.ArgumentParser(description="Поднять парную сессию целиком")
    parser.add_argument("--task", default="", help="задача для пары")
    parser.add_argument("--cli", default="opencode", help="CLI для обоих окон")
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
        print("не задана, остаётся текущая (по умолчанию semi)")

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
    run([ROOT / "bin" / "pair-duo", args.cli] + ([args.task] if args.task else []))

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