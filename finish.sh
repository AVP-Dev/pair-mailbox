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
        print("could not read state - unknown whom to launch", file=sys.stderr)
        return
    policy = state.get("policy") if isinstance(state.get("policy"), dict) else {}
    writer_role = policy.get("writer") or state.get("writer")
    critic_role = policy.get("critic")
    if not critic_role:
        known = [r for r in (state.get("roles") or {}) if r != writer_role]
        critic_role = known[0] if known else None
    if not writer_role:
        print("no writer in policy: `bin/pairctl roles --writer <role>`", file=sys.stderr)
        return
    if not critic_role:
        print("no critic in policy: `bin/pairctl roles --critic <role>`", file=sys.stderr)
        return
    if writer_role == critic_role:
        print("writer and critic are the same role - the pair cannot work like that", file=sys.stderr)
        return

    missing = [r for r in (writer_role, critic_role) if not launch_command_for(r)]
    if missing:
        print(f"no launch command in cli-profiles.json for: {', '.join(missing)}", file=sys.stderr)
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
    print(f"standing: writer {writer_role}, critic {critic_role}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Bring up the whole pair session")
    parser.add_argument("--task", default="", help="task for the pair")
    parser.add_argument("--cli", default="opencode",
                        help="CLI for both panes; only with --duo")
    parser.add_argument("--duo", action="store_true",
                        help="launch two copies of one CLI instead of the standing pair. "
                             "Hands write authority to <cli>-writer, so only by "
                             "explicit human choice")
    parser.add_argument("--no-prune", action="store_true",
                        help="do not prune dead roster entries")
    parser.add_argument("--no-pair", action="store_true",
                        help="install and verify but do not launch agents")
    parser.add_argument("--state", default=os.environ.get("PAIR_STATE", ".pair"))
    parser.add_argument("--autonomy", default="", choices=("", "observe", "semi", "auto"))
    parser.add_argument("--roles", default="", help="writer; writer,critic comma form also accepted (critic ignored, writer-pin)")
    args = parser.parse_args()

    failed = []

    step("1. tests before install")
    for suite in ("mcp/pair_mailbox/test_protocol.py", "bin/pairpilot_test.py",
                  "install_test.py"):
        if not python_ok([suite]):
            failed.append(suite)
    if failed:
        print(f"\nFAILED: {', '.join(failed)}")
        print("Fix the tests first. The installer touches your configs, and on")
        print("red code it will leave the machine in an intermediate state.")
        return 1

    step("2. install")
    install = run([sys.executable, ROOT / "install.sh"])
    if install.returncode != 0:
        failed.append("install.sh")

    step("3. standing roles")
    if args.roles and "," in args.roles:
        # Старая форма writer,critic: критик проигнорирован (writer-pin),
        # пишется только писатель — старые вызовы не падают.
        writer = args.roles.split(",", 1)[0].strip()
        run([sys.executable, ROOT / "mcp/pair_mailbox/server.py",
             "--state", args.state, "roles", "--writer", writer])
    elif args.roles:
        run([sys.executable, ROOT / "mcp/pair_mailbox/server.py",
             "--state", args.state, "roles", "--writer", args.roles.strip()])

    step("4. autonomy")
    if args.autonomy:
        run([sys.executable, ROOT / "mcp/pair_mailbox/server.py",
             "--state", args.state, "autonomy", args.autonomy])
    else:
        print("not set, keeping current (fresh default auto)")

    if args.no_pair:
        print("\nDone: installed and verified. Agents not launched (--no-pair).")
        return 1 if failed else 0

    step("5. supervisor BEFORE agents")
    if shutil.which("tmux") is None:
        print("tmux not found. Live delivery is impossible without it.")
        return 1
    pilot = subprocess.Popen([sys.executable, str(ROOT / "bin" / "pairpilot"),
                              "--state", args.state],
                             cwd=str(ROOT), stdout=open(ROOT / ".pair" / "pairpilot.log", "a"),
                             stderr=subprocess.STDOUT, start_new_session=True)
    print(f"supervisor: pid {pilot.pid}, log .pair/pairpilot.log")

    step("6. agents in fresh windows")
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
        print(f"duo-mode: two copies of {args.cli}, writer will be {args.cli}-writer")
        run([ROOT / "bin" / "pair-duo", args.cli] + ([args.task] if args.task else []))
    else:
        launch_standing_pair(args)

    step("7. result")
    time.sleep(8)  # агентам нужно время на регистрацию в MCP
    from server import Store, is_pid_alive, pane_exists  # noqa: E402

    state = Store(ROOT / args.state).read()
    roles = state.get("roles") or {}
    writer = state.get("writer")
    print(f"task:     {state.get('task') or 'NOT SET'}")
    print(f"phase:    {state.get('phase')}   mode: {state.get('mode')}"
          f"   autonomy: {state.get('autonomy')}")
    print(f"writer:   {writer or 'NOT ASSIGNED'}")
    print("\nrole         pane     pid     state")
    for name, info in sorted(roles.items()):
        pane = info.get("pane")
        pid = info.get("pid")
        alive = is_pid_alive(pid) if pid else None
        if not pane:
            note = "NO PANE - delivery impossible"
        elif not pane_exists(pane):
            note = "pane dead"
        elif alive is False:
            note = "process dead (supervisor will restart)"
        else:
            note = "alive"
        print(f"{name:12} {str(pane):8} {str(pid):7} {note}")

    print(f"\nsupervisor: {'running, pid ' + str(pilot.pid) if pilot.poll() is None else 'DEAD'}")
    print("chat:     bin/pairchat")
    print("stop:     kill " + str(pilot.pid))

    if failed:
        print(f"\nFAILED: {', '.join(failed)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
