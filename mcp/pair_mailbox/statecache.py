"""Отпечаток состояния для кэша `pair_state`.

Полный ответ `pair_state` стоит ~939 токенов, а агенты зовут его несколько раз за
раунд, хотя состояние между зовами обычно не меняется. Кэш отвечает короткой
заглушкой — но только если отпечаток действительно изменился.

Модуль вынесен из `server.py` отдельно оттого, что зависит только от того, что
чи��ает ответ, а не от того, как ответ собирается. `server.py` импортирует его
по той же причине, что `wake.py` и `review.py`: показывать, что он остаётся
единственным местом, где живёт сессия.

Направление зависимости то же, что у остальных модулей: `server.py` зависит от
этого файла, не наоборот.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Callable

# Поля, которые меняются при каждом обращении к состоянию сами по себе и потому
# не должны ронять кэш: их отсутствие в отпечатке — не потеря информации о том,
# что агент способен увидеть, иначе кэш не срабатывал бы никогда.
VOLATILE_STATE_KEYS = ("updated_at", "roles")
VOLATILE_ROLE_KEYS = ("last_seen",)

CACHE_DISABLED_ENV = "PAIR_DISABLE_STATE_CACHE"
UNCHANGED_NOTE = (
    "Состояние не изменилось с прошлого вызова. Читай pair_digest для ориентации."
)


def cache_disabled(environ: dict[str, str] | None = None) -> bool:
    """Выключатель кэша.

    Обещан комментарием и подключён здесь: тесты, которым нужен живой ответ на
    каждый вызов, ставят переменную, иначе проверка закрывающего факта читала бы
    заглушку и проходила, ничего не проверяя.
    """
    env = environ if environ is not None else __import__("os").environ
    return bool(env.get(CACHE_DISABLED_ENV))


def fingerprint(
    state: dict[str, Any],
    *,
    obligation_names: list[str] | None = None,
    inbox_names: list[str] | None = None,
    git_snapshot: Callable[[], Any] | None = None,
    alive: dict[str, bool | None] | None = None,
) -> str:
    """Короткий отпечаток всего, что читает полный ответ `pair_state`.

    Поля вручную не перечисляем: ответ зависит от evidence, human_ok, verdicts,
    patches, roles, seen и от файлов обязательств — они живут на диске и в
    `state.json` не попадают. Промах по любому из них возвращал заглушку без
    `obligations` и `evidence_this_round`, то есть без ровно тех полей, ради
    которых агент звал `pair_state`.

    Исключены только `updated_at` и `last_seen`: они меняются при каждом
    регистрировании наравне с чтением и не несут для агента ничего, чего не
    видно из остального состояния.

    Имена файлов обязательств и входящих передаются списками, а не читаются
    здесь: модуль не должен знать, где они лежат, — этим владеет сессия.
    То же с `alive`: `peers.alive` читается живым `os.kill(pid, 0)`, а не
    номером в state — процесс умирает снаружи, state не меняется, и без
    этого снимка заглушка врала бы, что мёртвый сосед слушает. Сессия
    снимает живость сама и передаёт готовую — тогда модуль не знает
    про процессы вовсе.

    `git_snapshot` вызывается только когда есть вердикты: `_done_blockers`
    читает снимок HEAD+dirty для проверки stale, и отпечаток обязан это видеть.
    Вызывающий сам решает, готов ли снимок, и передаёт готовый — тогда модуль не
    знает про git вовсе.
    """
    roles = {
        name: {k: v for k, v in info.items() if k not in VOLATILE_ROLE_KEYS}
        for name, info in (state.get("roles") or {}).items()
        if isinstance(info, dict)
    }
    git_fp = None
    if state.get("verdicts") and git_snapshot is not None:
        git_fp = git_snapshot()
    cache_key = {
        "state": {k: v for k, v in state.items() if k not in VOLATILE_STATE_KEYS},
        "roles": roles,
        "obligations": sorted(obligation_names or []),
        "inbox": list(inbox_names or []),
        "git": git_fp,
        "alive": dict(sorted((alive or {}).items())),
    }
    return hashlib.sha256(json.dumps(cache_key, sort_keys=True).encode()).hexdigest()[:12]


def unchanged_answer(state_hash: str) -> dict[str, Any]:
    """Короткий ответ на неизменившееся состояние."""
    return {"unchanged": True, "hash": state_hash, "note": UNCHANGED_NOTE}