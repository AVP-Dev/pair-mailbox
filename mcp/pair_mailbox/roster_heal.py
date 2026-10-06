"""Самозалечивание ростера: перепроверка панелей и истечение мёртвых записей (#19.1, #19.3).

`_register` в server.py вызывается перед каждым инструментом, но перепроверял
только панель ВЫЗЫВАЮЩЕГО: env-панель до записи (pane_env_rejected) и чужие
панели лишь когда своей нет (pane_foreign_dropped). Пока свой агент жив,
записи соседей не трогались — протухшая панель висела до ручного
`pairctl roster --prune`. Этот модуль — та самая перепроверка, вынесенная
из сервера: server владеет mutate/lock, panes владеет tmux-вердиктами,
а здесь решение «снять pane / удалить запись» по готовым фактам.

Два независимых факта — принцип всего модуля, как в 19.2 и в prune:
`os.kill(pid, 0)` доказывает существование процесса, а не владение слотом
(pid переиспользуется), а `%N` перенумеровывается. Один факт = ложный отказ
или фантом. Поэтому снятие идёт только при мёртвом pid И подтверждённой
проверке панели, а удаление записи — только при отсутствии pane И мёртвом
pid И простое дольше TTL.

Fail-closed везде: tmux недоступен или проверка не удалась — запись остаётся.
Временный провал tmux никогда не стирает данные (урок #10: pane_pin стирали
навсегда после временного провала). Закрепление человека (pane_pin) не
трогается никогда: переопределение выигрывает у любой автоматики.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# Сколько молчит мёртвая запись без панели, прежде чем истечь. 7 суток:
# короче — убьёт контекст после отпуска, дольше — хлам копится. Ручной
# `pairctl roster --prune` остаётся мгновенной чисткой, TTL — страховкой
# от вечного хлама. Env для тестов, чтобы не ждать неделю.
ROLE_TTL_SECONDS = float(os.environ.get("PAIR_ROLE_TTL_SECONDS", "604800") or 604800)


def _age_seconds(last_seen: Any) -> float | None:
    """Сколько секунд прошло с last_seen. Неизмеримо — None, а не 0."""
    if not last_seen or not isinstance(last_seen, str):
        return None
    try:
        return (datetime.now(timezone.utc)
                - datetime.fromisoformat(last_seen)).total_seconds()
    except (ValueError, TypeError):
        return None


def recheck_peers(
    roles: dict[str, Any],
    self_role: str,
    project_root: Path,
    *,
    pid_alive: Callable[[Any], bool],
    pane_exists: Callable[[Any], bool],
    belongs_to_project: Callable[..., tuple],
    same_pane: Callable[[Any, Any], bool],
) -> list[dict[str, Any]]:
    """Перепроверить записанные панели соседей (#19.1). Возвращает события.

    Мутирует записи на месте (снять `pane`), вызывающий пишет события
    в транскрипт и сохраняет состояние. Событие: {"kind", "role",
    "other_role", ...поля для транскрипта}.
    """
    events: list[dict[str, Any]] = []
    living_panes = [
        info.get("pane") for name, info in roles.items()
        if name != self_role and info.get("pane")
        and isinstance(info.get("pid"), int) and pid_alive(info["pid"])
    ]
    for name, info in roles.items():
        if name == self_role:
            continue
        pane = info.get("pane")
        if not pane or info.get("pane_pin"):
            # Нечего проверять или закреплено человеком — пропуск.
            continue
        pid = info.get("pid")
        if isinstance(pid, int) and pid > 0 and pid_alive(pid):
            # Живой процесс перерегистрируется сам. Лечить чужую живую
            # запись — значит отбирать панель у работающего агента.
            continue
        try:
            if not pane_exists(pane):
                info.pop("pane", None)
                info.pop("pane_pin_why", None)
                events.append({"kind": "pane_stale_dropped", "role": self_role,
                               "other_role": name, "pane": pane,
                               "why": "панель отсутствует в tmux, процесс мёртв"})
                continue
        except Exception:
            # Проверить нечем — не значит «адрес мёртв» (урок pin UNKNOWN).
            continue
        try:
            ours, why_not = belongs_to_project(pane, project_root)
        except Exception:
            continue
        if not ours:
            info.pop("pane", None)
            info.pop("pane_pin_why", None)
            events.append({"kind": "pane_foreign_dropped", "role": self_role,
                           "other_role": name, "pane": pane, "why": why_not})
            continue
        if any(same_pane(held, pane) for held in living_panes if held):
            info.pop("pane", None)
            info.pop("pane_pin_why", None)
            events.append({"kind": "pane_stale_dropped", "role": self_role,
                           "other_role": name, "pane": pane,
                           "why": "панель занята другой живой ролью"})
    return events


def expire_dead_roles(
    roles: dict[str, Any],
    writer: Any,
    *,
    pid_alive: Callable[[Any], bool],
    ttl_seconds: float = ROLE_TTL_SECONDS,
) -> list[dict[str, Any]]:
    """Удалить мёртвые записи без панели после TTL молчания (#19.3).

    Условия удаления, все сразу: нет pane; pid мёртв или отсутствует;
    last_seen старше TTL; не держатель права записи. Возвращает события
    {"kind": "role_expired", ...} для транскрипта.
    """
    events: list[dict[str, Any]] = []
    for name, info in list(roles.items()):
        if info.get("pane") or info.get("pane_pin"):
            continue
        pid = info.get("pid")
        if isinstance(pid, int) and pid > 0 and pid_alive(pid):
            continue
        if name == writer:
            # Право записи не испаряется по таймеру (как в prune).
            continue
        age = _age_seconds(info.get("last_seen"))
        if age is None or age < ttl_seconds:
            # Возраст неизмерим (старые записи без last_seen) — fail-closed.
            continue
        roles.pop(name, None)
        events.append({"kind": "role_expired", "role": name,
                       "pid": pid, "age_seconds": int(age),
                       "why": "мёртвая запись без панели истекла по TTL"})
    return events
