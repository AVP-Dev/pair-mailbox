"""Привязка ролей к панелям tmux: `pairctl pane` и `pairctl roster`.

Вынесено из `server.py` отдельной ответственностью. Здесь живут обе команды,
которые читают и меняют адрес панели, и обе они проходят через одну и ту же
проверку `pin_verdict`. Держать их рядом — не про порядок строк, а про
безопасность: «эта роль сидит вот здесь» не может быть отделано от «а куда
вообще печатать» так, чтобы одно из двух оказалось забытым.

Особая оговорка про fail-closed, и она относится именно к этому файлу.
`pin_verdict` отдаёт три состояния: `ok`, `bad` (проверено, что адрес мёртв или
чужой) и `unknown` (проверить нечем — tmux недоступен). Первое и последнее
различаются здесь, а не в самой проверке, потому что решение принимает вызывающий,
и решения у двух вызывающих разные:

- `pairctl pane` отказывает и на `bad`, и на `unknown`: человек ещё не выбрал
  адрес, и записывать непроверяемый нельзя;
- `_register` в `server.py` снимает закрепление только на `bad`, а на `unknown`
  оставляет его в силе и не печатает в эту панель. Провал tmux на пять секунд
  не должен отменять решение человека навсегда.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

from server import Store, is_pid_alive, pane_exists, same_pane, utcnow

# Три состояния, а не два, и разница между ними не стилистическая.
#
# `bad` — «проверено, что адрес мёртв или ведёт не сюда». Это единственное
# состояние, в котором закрепление снимается. `unknown` — «проверить нечем»: tmux
# недоступен или не ответил. Раньше и то и другое возвращало один refused, а
# `_register` снимал закрепление на ЛЮБОЙ отказ, то есть провал tmux на пять секунд
# стирал решение человека навсегда.
#
# Fail-closed верен там, где решение принимается: печатать в панель, которую нечем
# проверить, нельзя. Но стирать закрепление по временной негодности нельзя тоже:
# «не подтверждено» и «опроверено в негодность» это разные вещи.
PIN_OK = "ok"
PIN_BAD = "bad"
PIN_UNKNOWN = "unknown"


def pin_verdict(tmux, pin: str, project_root: Path) -> tuple[str, str]:
    """Годится ли закреплённая панель, и если нет — почему.

    Переопределение человеком («эта роль сидит вот здесь») обязано проходить ту же
    проверку, что и адрес, снятый с панели автоматически. Иначе `pairctl pane`
    стал бы дырой в изоляции, ради которой он и затевался: один неверный адрес,
    и письмо роли уходит в чужой проект — ровно то, что чинили пункты 1 и 2.

    Два независимых отказа, и они не взаимозаменяемы:
    - панели нет (`pane_alive`) — ссылка протухла;
    - панель чужая (`pane_is_foreign`) — ссылка жива, но ведёт не сюда.

    Оба дают `PIN_BAD`. Отсутствие tmux (или его сбой на панели) даёт `PIN_UNKNOWN`:
    подтвердить нечем, но и опровергнуть тоже. Вызывающий решает по-своему:
    `pairctl pane` отказывает (записать непроверяемый адрес нельзя), а `_register`
    оставляет закрепление в силе и просто не печатает в эту панель.
    """
    if not pin:
        return PIN_BAD, "адрес не задан"
    if not tmux:
        return PIN_UNKNOWN, "tmux недоступен, проверить нечем"
    from composer import pane_alive, pane_is_foreign
    if not pane_alive(tmux, pin):
        return PIN_BAD, "панели по этому адресу больше нет"
    try:
        foreign, why_foreign = pane_is_foreign(tmux, pin, project_root)
    except Exception as exc:  # noqa: BLE001 - временный сбой tmux не приговор адресу
        return PIN_UNKNOWN, f"панель не прочиталась: {exc}"
    if foreign:
        return PIN_BAD, f"панель чужая: {why_foreign}"
    return PIN_OK, "панель жива и наша"


def cmd_pane(store: Store, args: argparse.Namespace) -> int:
    # Переопределение привязки панели рукой человека.
    #
    # Зачем оно, если адрес и так снимается с панели: `_register` берёт адрес из
    # `TMUX_PANE` окружения процесса, и у сервера, который сам себя перерегистри-
    # стрируется после рестарта, это окружение бывает чужим или протухшим. Правка
    # state.json вручную тут не помогает: `_register` перезаписывает `pane` на
    # каждом вызове, и «починка» живёт до следующего звонка. Ключ `pane_pin`
    # читается раньше окружения, поэтому выигрывает.
    #
    # Адрес проверяется той же мерой, что и снятый автоматически, иначе
    # переопределение стало бы дырой в изоляции между проектами.
    tmux = shutil.which("tmux")
    # Две формы вызова, одна семантика (карточка pairctl-pane-missing):
    #   pairctl pane --role <role> [address] [--clear]   # исходная, остаётся
    #   pairctl pane <role> [address]                    # позиционная из карточки
    # Без address — только показ, без записи: `pane <role>` из живой панели
    # обязан отвечать, а не закреплять. Привязка к текущей панели — только
    # явным флагом `--current`: иначе последовательные опросы двух ролей из
    # одного окна молча сажают обе на одну панель, и супервизор пропускает
    # их как неразличимые (shared_panes -> skip). Причина измерена: %2205
    # за opencode и pi после `pane opencode` + `pane pi` из ml:0.0.
    positionals = list(getattr(args, "positional", None)
                       or ([getattr(args, "address", None)]
                           if getattr(args, "address", None) else []))
    role = getattr(args, "role", None) or ""
    address = None
    if role and positionals:
        address = positionals[0]
    elif not role and len(positionals) >= 2:
        role, address = positionals[0], positionals[1]
    elif not role and len(positionals) == 1:
        role = positionals[0]
    if not role:
        print("error: укажи роль: `pane <role> [address]` или `pane --role <role> [address]`",
              file=sys.stderr)
        return 2
    if not address and getattr(args, "current", False) and not args.clear:
        env_pane = (os.environ.get("TMUX_PANE") or "").strip()
        if env_pane:
            address = env_pane
    state = store.read()
    roles_now = state.setdefault("roles", {})
    if role not in roles_now:
        print(f"warning: {role!r} has never connected; registered roles: "
              f"{', '.join(sorted(roles_now)) or '—'}")

    if args.clear:
        if not roles_now.get(role, {}).get("pane_pin"):
            print(f"у роли {role} и так нет переопределения панели")
            return 0
        store.mutate(lambda st: (st["roles"][role].pop("pane_pin", None),
                                 st["roles"][role].pop("pane_pin_why", None)))
        store.append_transcript("pane_pin_cleared", role, by="human")
        print(f"переопределение панели роли {role} снято, снова берётся из tmux")
        return 0

    if not address:
        info = roles_now.get(role, {})
        if not info:
            print(f"роль {role} не подключалась; панель неизвестна")
            return 1
        pin = info.get("pane_pin")
        if pin:
            print(f"{role} закреплена за {pin}")
            print(f"  {info.get('pane_pin_why') or 'причина не записана'}")
            print(f"  записано в ростер: {info.get('pane')}")
        else:
            print(f"{role} переопределения не имеет, панель из tmux: "
                  f"{info.get('pane') or '—'}")
        return 0

    verdict, why = pin_verdict(tmux, address, store.project_root)
    if verdict != PIN_OK:
        # Отказ здесь обязателен и одинаков для `bad` и `unknown`: записать
        # негодный адрес — значит на следующем же вызове `pair_deliver` уйти в
        # чужой проект или в никуда, а записать непроверяемый — значит
        # положить в состояние то, о чём мы не знаем. Разница между ними
        # проявится позже, в `_register`: `bad` сотрёт закрепление,
        # `unknown` оставит его в силе.
        print(f"error: {address!r} не годится: {why}", file=sys.stderr)
        return 1

    def apply_pin(st):
        entry = st.setdefault("roles", {}).setdefault(
            role, {"first_seen": utcnow(), "reads": 0, "sends": 0})
        previous = entry.get("pane")
        entry["pane_pin"] = address
        entry["pane_pin_why"] = f"закреплено человеком: {why}"
        # Сразу записываем в `pane`, чтобы переопределение начало действовать
        # без ожидания следующего вызова инструмента, а не только после него.
        entry["pane"] = address
        entry["last_seen"] = entry.get("last_seen") or utcnow()
        return previous

    previous = store.mutate(apply_pin)
    store.append_transcript("pane_pin_set", role, by="human",
                            pane=address, was=previous, why=why)
    print(f"{role} закреплена за {address} ({why})")
    if previous and previous != address:
        print(f"  было: {previous}")
    return 0


def cmd_panes(store: Store, args: argparse.Namespace) -> int:
    # Живые панели tmux -> проект (cwd) -> роль из ростера -> живость pid.
    # Только чтение: состояние не трогает, в журнал не пишет. Ответ на вопрос
    # «какая панель чья», который раньше требовал ps + tmux + state.json.
    import subprocess
    tmux = shutil.which("tmux")
    if not tmux:
        print("tmux недоступен, панели не прочитать")
        return 1
    try:
        out = subprocess.run(
            [tmux, "list-panes", "-a", "-F",
             "#{pane_id}\t#{session_name}:#{window_index}.#{pane_index}\t#{pane_current_path}\t#{pane_current_command}\t#{pane_pid}\t#{pane_title}"],
            capture_output=True, text=True, timeout=10)
    except Exception as exc:  # noqa: BLE001 - tmux упал, сказать а не врать
        print(f"tmux не ответил: {exc}")
        return 1
    if out.returncode != 0:
        print(f"tmux отказал: {(out.stderr or '').strip()[:120]}")
        return 1
    state = store.read()
    roles_now = state.get("roles") or {}
    # Панель -> роли этого проекта, что на неё записаны.
    by_pane: dict[str, list[str]] = {}
    for role, info in roles_now.items():
        pane = info.get("pane")
        if pane:
            by_pane.setdefault(str(pane), []).append(role)
    lines = out.stdout.splitlines()
    if not lines:
        print("живых панелей нет")
        return 0
    print(f"{'панель':14} {'чей':7} {'проект (cwd)':36} {'роль':12} {'процесс'}")
    for line in lines:
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        uid, addr, cwd, cmd, pid = parts[0], parts[1], parts[2], parts[3], parts[4]
        title = parts[5] if len(parts) > 5 else ""
        ours = ""
        try:
            ours = "НАШ" if Path(cwd).resolve() == store.project_root.resolve() \
                or store.project_root.resolve() in Path(cwd).resolve().parents else "чужой"
        except OSError:
            ours = "?"
        claim = ",".join(sorted(by_pane.get(uid, []) + by_pane.get(addr, []))) or "—"
        if title and title not in ("", cmd):
            claim += f" [{title}]"
        alive = "жив" if pid.strip().isdigit() and is_pid_alive(int(pid.strip())) else "?"
        print(f"{addr:14} {ours:7} {cwd[-34:]:36} {claim:12} {alive} {cmd}")
    print(f"\n{len(lines)} панелей; проект: {store.project_root} (НАШ — этот проект)")
    return 0


def cmd_roster(store: Store, args: argparse.Namespace) -> int:
    #
    # `roster` без флагов только показывает. `--prune` удаляет, и удаляет по
    # двум независимым фактам сразу: процесс мёртв И панели больше нет. Одного
    # факта мало — умерший агент мог оставить после себя живую панель, а живой
    # pid с чужой панелью означает, что запись указывает не туда.
    #
    # Удалять молча нельзя: ростер это общая память пары, и человек должен
    # видеть, что именно убрали и почему.
    current = store.read()
    roles_now = current.get("roles") or {}
    # Кто живой и на какой панели. Нужно, чтобы отличить «панели нет» от
    # «панель есть, но она не эта».
    living = [role for role, info in roles_now.items()
              if isinstance(info.get("pid"), int) and info["pid"] > 0
              and is_pid_alive(info["pid"])]
    entries = []
    for role, info in roles_now.items():
        pid = info.get("pid")
        pane = info.get("pane")
        pid_dead = isinstance(pid, int) and pid > 0 and not is_pid_alive(pid)
        pane_gone = bool(pane) and not pane_exists(pane)
        # Разные форматы одной панели не должны выглядеть как разные панели,
        # иначе `--prune` удалил бы запись, просто потому что сосед записан
        # адресом сессии, а этот — номером.
        holder = next((other for other in living
                       if other != role
                       and same_pane(roles_now[other].get("pane"), pane)), None)
        entries.append((role, info, pid_dead, pane_gone or holder is not None, holder))
    if not args.prune:
        if not entries:
            print("ростер пуст")
            return 0
        for role, info, pid_dead, wrong, holder in sorted(entries):
            mark = "жив" if not pid_dead else "мёртв"
            extra = ", панели нет" if wrong and not holder else ""
            if holder:
                extra = f", панель занята живой ролью {holder}"
            print(f"{role:18} pid={info.get('pid')} pane={info.get('pane') or '—'}  {mark}{extra}")
        print("\n--prune удалит записи с мёртвым pid, у которых панели нет "
              "или панель принадлежит другой живой роли")
        return 0

    doomed = {role: True for role, _, pid_dead, wrong, _ in entries
              if pid_dead and wrong}
    if not doomed:
        print("нечего чистить: нет записей одновременно с мёртвым pid и без панели")
        return 0
    if current.get("writer") in doomed:
        print(f"refusing to prune {current['writer']!r}: it still holds write authority. "
              f"Move it with `pairctl assign` or `pairctl release` first.",
              file=sys.stderr)
        return 2

    def prune(state):
        roles = state.get("roles") or {}
        for role in doomed:
            roles.pop(role, None)
        state["roles"] = roles

    store.mutate(prune)
    store.append_transcript("roster_pruned", "human", removed=sorted(doomed))
    for role in sorted(doomed):
        print(f"удалён {role}")
    return 0