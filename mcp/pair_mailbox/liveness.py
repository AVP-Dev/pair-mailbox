"""Живость и готовность соседа читать сообщения.

`_peer_status` отвечал на вопрос, который задают себе оба агента в каждом раунде:
сосед ещё здесь, и прочитает ли он то, что я сейчас напишу. Ответ нужен читателю
состояния, а не тому, кто состояние меняет, поэтому он и живёт здесь.

Модуль ничего не знает про `PairSession`: на входе состояние и собственная роль,
на выходе — словарь по ролям. Именно поэтому его можно вызывать и из теста, не
поднимая сервер.
"""
from __future__ import annotations

import errno
import os
from datetime import datetime, timezone
from typing import Any


def pid_alive(pid: Any) -> bool:
    """Жив ли процесс с таким pid.

    Отдельная функция, а не обращение к серверу: тест живости соседа не должен
    ради проверки одной строки поднимать MCP-сервер целиком.

    Два случая неочевидны и оба перенесены из прежнего `server.is_pid_alive`
    буквально, потому что теряются при переписывании:

    - pid меньше единицы и не-целое число неprocess. `os.kill(-1, 0)` на Linux
      успешно «проверяет» все процессы разом и вернул бы alive, то есть соседа
      с испорченной записью объявили бы живым;
    - EPERM значит «процесс есть, но не мой». Это alive, а не мёртв: сосед может
      быть запущен от другого пользователя, и объявлять его мёртвым — значит
      не ждать ответа, который придёт.
    """
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError as err:
        return err.errno == errno.EPERM
    return True


def peer_status(
    state: dict[str, Any],
    *,
    role: str,
    writer: str | None,
    alive_check=pid_alive,
) -> dict[str, Any]:
    """Что делает каждый другой участник и услышит ли он новое.

    Писатель раньше закрывал раунд, не имея понятия, ушёл ли сосед, и тогда
    находка приезжала в закрытый раунд. Сказать об этом вслух дешевле, чем
    складывать письмо в никуда.

    `alive_check` подменяется в тестах: проверка «сосед мёртв» не должна зависеть
    от того, жив лиPID в момент прогона набора.
    """
    phase = state.get("phase", "scoping")
    now = datetime.now(timezone.utc)
    out: dict[str, Any] = {}
    for name, info in (state.get("roles") or {}).items():
        if name == role:
            continue
        age = None
        try:
            if info.get("last_seen"):
                age = int((now - datetime.fromisoformat(info["last_seen"])).total_seconds())
        except Exception:
            age = None
        closed = phase == "done"
        pid = info.get("pid")
        alive = alive_check(pid) if pid else None
        hint = "Listening."
        if closed:
            hint = (
                "The round is closed. This role has moved on and will not read new"
                " messages. To get it back: pair_advance('implement')."
            )
        elif alive is False:
            hint = f"Process is no longer running (PID {pid} is dead)."
        out[name] = {
            "role": "writer" if name == writer else "critic",
            "last_seen": info.get("last_seen"),
            "silent_seconds": age,
            "alive": alive,
            "round_closed": closed,
            "will_read_new_messages": not closed and (alive is not False),
            "hint": hint,
        }
    return out