"""Живость того, что живёт на этой машине: панели tmux, pid, процесс сверхвизора.

Отдельный модуль, потому что это другая порода фактов, чем записанное протоколом.
`board_snapshot` читает то, что пара записала о себе: фаза, роли, задачи, журнал.
Здесь — то, что можно узнать, только спросив систему: жив ли процесс, есть ли
панель, когда файлы менялись. Смешивать их в одном модуле означало бы, что тест
снимка обязан иметь под рукой настоящий tmux, а на машине без него проверка живости
сверхвизора прошла бы, ни разу не дойдя до `/proc`.

Контракт честности: «не знаю» и «нет» — разные ответы, и разными они остаются на
всех трёх уровнях.

- панели: `pane_alive` может не ответить (нет tmux, панель не той формы), и это
  `None`, а не `False`;
- pid: отсутствие записи — это `None`, а не «процесс мёртв»;
- сверхвизор: «процесс не найден» означает ровно то, что написано.

Модуль ничего не пишет и не чинит: это наблюдатель, как и весь дашборд.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pairctl_read import human_age

PILOT_LOG = "pairpilot.log"
PILOT_REPORT = "REPORT.md"


def mtime_age(path: Path) -> float | None:
    """Сколько секунд назад файл менялся. None, если файла нет или он недоступен."""
    try:
        stamp = datetime.fromtimestamp(Path(path).stat().st_mtime, timezone.utc)
    except OSError:
        return None
    return max(0.0, (datetime.now(timezone.utc) - stamp).total_seconds())


def pane_status(pane: Any) -> tuple[bool | None, str]:
    """Жива ли панель роли. (None, причина) — отвечать «не знаю», а не «нет».

    `composer` импортируется лениво: он тянет `os` и `re`, но на верхнем уровне
    перехватывал бы импорт для всех, включая сборку снимка без tmux вообще. Если
    tmux нет или он упал, это отсутствие данных, а не мёртвая панель: красная
    точка на живой роли отправит человека чинить то, что не сломано.
    """
    text = str(pane or "").strip()
    if not text:
        return None, "панель не записана"
    try:
        from composer import _tmux, pane_alive
    except Exception as error:  # noqa: BLE001 - причина должна быть видна в ленте
        return None, f"tmux недоступен: {error}"
    try:
        return (True if pane_alive(_tmux(), text) else False), ""
    except Exception as error:  # noqa: BLE001 - панель лучше проверить, чем упасть
        return None, str(error)


def clean_pid(value: Any) -> int | None:
    """Настоящий pid или None. Ноль и мусор — это «не записано», а не «мёртв».

    `server.is_pid_alive` на малом числе и на не-целом отвечает False, и в снимок
    это уехало бы как «процесс мёртв» у роли, у которой pid просто никто не писал.

    `bool` отсекается явно: в Python `isinstance(True, int)` истинно, и без этой
    проверки `True` прошёл бы как pid 1 — то есть как init.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def pilot_pids(script: str) -> list[int]:
    """Все живые процессы, в чьей командной строке встречается `script`.

    Отдельная публичная функция, а не часть `pilot_section`: её содержательная
    часть — обход `/proc`, и проверяется он отдельно, иначе тест живости
    сверхвизора прошёл бы, ни разу не дойдя до обхода. Свой pid пропускается —
    вызывающий нашёл бы себя и объявил сверхвизором, а это тот вид лжи, который
    хуже отсутствия данных.

    Сверхвизор не пишет pid в состояние — он владеет процессом, и отдельная запись
    «пилот жив» протухала бы вслед за падением. Ищется по всему
    `/proc/<pid>/cmdline`, потому что имя скрипта там может быть относительным:
    ровно так его запускает `finish.sh`.
    """
    found: list[int] = []
    try:
        names = os.listdir("/proc")
    except OSError:
        return found
    for name in names:
        if not name.isdigit() or int(name) == os.getpid():
            continue
        try:
            raw = Path("/proc", name, "cmdline").read_bytes()
        except OSError:
            continue
        if script.encode() in raw:
            found.append(int(name))
    return sorted(found)


def pilot_section(state_dir: Path, entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Жив ли сверхвизор и чем он занят.

    Ищется ИМЕННО наш `bin/pairpilot` в ИМЕННО этом проекте: чужой пилот из
    соседней рабочей копии не должен светиться зелёным здесь.

    Возраст файла отдаётся дважды: в секундах (пороги решает клиент) и словами
    (показывать). Форматирование секунд в текст — одна реализация на проект,
    `pairctl_read.human_age`, и вторая копия в JavaScript разъехалась бы с ней
    при первом же изменении порогов.
    """
    state_dir = Path(state_dir)
    log_path = state_dir / PILOT_LOG
    report_path = state_dir / PILOT_REPORT
    pids = pilot_pids(str(state_dir.parent / "bin" / "pairpilot"))
    log_age = mtime_age(log_path)
    report_age = mtime_age(report_path)

    pilot_events = [e for e in entries if str(e.get("kind", "")).startswith("pilot_")]
    refusals = sum(1 for e in pilot_events
                   if "refus" in str(e.get("kind")) or "impossible" in str(e.get("kind")))
    return {
        "running": bool(pids),
        "pid": min(pids) if pids else None,
        "pids": pids,
        "log_path": str(log_path),
        "log_size": log_path.stat().st_size if log_path.exists() else 0,
        "log_age_seconds": log_age,
        "log_age": human_age(log_age),
        "report_path": str(report_path),
        "report_exists": report_path.exists(),
        "report_age_seconds": report_age,
        "report_age": human_age(report_age),
        "events_shown": len(pilot_events),
        "refusals": refusals,
        "last": _last_summary(pilot_events),
        "last_ts": pilot_events[-1].get("ts") if pilot_events else None,
    }


def _last_summary(entries: list[dict[str, Any]]) -> str:
    """Что пилот делал последним. Своя однострочная выборка вместо импорта
    `summarise_event`: тот живёт в снимке и о тексте журнала, а тут нужен только
    факт «был повод» — подробности всё равно показывает лента событий ниже."""
    if not entries:
        return ""
    for field in ("why", "prompt", "value", "phase", "path"):
        if entries[-1].get(field) not in (None, "", {}, []):
            return str(entries[-1][field]).strip().replace("\n", " ")[:160]
    return str(entries[-1].get("kind") or "")