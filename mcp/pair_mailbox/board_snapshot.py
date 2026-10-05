"""Снимок состояния пары для статического дашборда: только данные, ни байта HTML.

Зачем отдельный слой. Дашборд открывается двойным щелчком по файлу, то есть
страница не имеет ни сервера, ни права читать `.pair/` сама: с `file://` браузер
запрещает `fetch()` локальных файлов. Единственный честный источник данных —
процесс, который прочитал состояние и вложил его в страницу; `board_render.py`
только превращает его вывод в страницу.

Почему арифметика «молчит» здесь, а не в `pairctl_read.silence_seconds`. Тот разбирает
весь `transcript.jsonl` целиком, потому что у него нет хвоста в руках. Здесь хвост уже
прочитан (`Store.read_transcript(limit)` читает с конца файла: 67.5 мс -> 0.4 мс на
файле в 2 МБ). Форматирование секунд в текст по-прежнему одно: `human_age`.

Про «жив» — в `board_hosts.py`: это вопросы к системе, а не к протоколу, и держать
их здесь означало бы тестировать снимок только при наличии tmux. Ничего не пишется:
дашборд — наблюдатель, и единственный появляющийся файл — сам HTML, который пишет
`bin/pairboard`.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from board_hosts import clean_pid, mtime_age, pane_status, pilot_section
from pairctl_read import human_age
from server import PHASES, Store, is_pid_alive, utcnow

# Сколько последних записей журнала попадает на страницу. Журнал растёт сам собой и
# на длинном раунде это сотни мегабайт; дашборд показывает картину, а не архив.
DEFAULT_EVENTS = 40

# После скольких секунд без пересборки снимок считается протухшим: страница
# показывает это меткой, а не продолжает изображать бодрую картину.
DEFAULT_STALE_AFTER = 90

# Обрезание длинных значений. Причины в evidence и `why` — абзацы, и в дашборд
# им не место: страница должна читаться, а не скроллиться.
MAX_VALUE_CHARS = 160

TASK_TEXT_CHARS = 300


def _age(stamp: Any) -> float | None:
    """Сколько секунд назад была метка времени. None, если разбирать нечего."""
    if not stamp:
        return None
    try:
        moment = datetime.fromisoformat(str(stamp))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - moment).total_seconds())


def _clip(value: Any, limit: int = MAX_VALUE_CHARS) -> str:
    text = str(value if value is not None else "").strip().replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def summarise_event(entry: dict[str, Any]) -> str:
    """Одна короткая строка на событие.

    Журнал разнороден: у `send` есть subject, у `evidence` — value, у `advance` —
    фаза. Выбор первого осмысленного поля даёт читаемую ленту, где ни одна запись
    не превращается в дамп json. Неизвестный вид остаётся именем самого вида:
    выдумывать смысл события, которого нет в словаре, нельзя.
    """
    for field in ("subject", "why", "value", "prompt", "reason", "text", "note",
                  "phase", "mode", "autonomy", "status", "path", "bell_kind",
                  "note_kind", "evidence_kind", "pane", "role"):
        if entry.get(field) not in (None, "", {}, []):
            return _clip(entry[field])
    return entry.get("kind", "?")


def _role_rows(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Роли: три факта о живости и молчание в человеческих единицах."""
    writer = state.get("writer")
    rows: list[dict[str, Any]] = []
    for name in sorted(state.get("roles") or {}):
        info = state.get("roles", {}).get(name) or {}
        pid = clean_pid(info.get("pid"))
        pid_alive = is_pid_alive(pid) if pid else False
        alive, why = pane_status(info.get("pane"))
        silence = _age(info.get("last_seen"))
        rows.append({
            "role": name,
            "is_writer": name == writer,
            "pane": str(info.get("pane") or ""),
            "pane_alive": alive,
            "pane_note": why,
            "pid": pid,
            "pid_alive": pid_alive,
            "last_seen": info.get("last_seen"),
            "silence_seconds": silence,
            "silence": human_age(silence),
            "sends": int(info.get("sends") or 0),
            "reads": int(info.get("reads") or 0),
            "seen": int((state.get("seen") or {}).get(name) or 0),
            "restored_by": _clip(info.get("pane_restored_by")),
        })
    # Писатель первым: это тот, за кем смотрят в первую очередь.
    rows.sort(key=lambda row: (not row["is_writer"], row["role"]))
    return rows


def _state_read_error(state_dir: Path) -> str:
    """Почему состояние не читается как состояние. Пустая строка — читается.

    Отдельная проверка нужна потому, что `Store.read()` битый `state.json` не
    поднимает, а тихо заменяет дефолтами. Для протокола это верно: сервер не должен
    падать из-за файла. Для ДАШБОРДА это ложь: страница показала бы пустую фазу и
    ноль ролей, и человек решил бы, что пара потеряла агентов, хотя файл просто
    повреждён. Цена — один проход по маленькому файлу, польза — правда на экране.
    """
    path = Path(state_dir) / "state.json"
    if not path.exists():
        return ""
    try:
        json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return f"state.json не читается: {error}"
    return ""


def _task_rows(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Задачи: открытые первыми, внутри — от новых к старым."""
    rows = []
    for item in state.get("tasks") or []:
        rows.append({
            "id": item.get("id"),
            "text": _clip(item.get("text"), TASK_TEXT_CHARS),
            "status": str(item.get("status") or "open"),
            "round": item.get("round"),
            "added_by": item.get("added_by") or "",
            "added_at": item.get("added_at"),
            "closed_by": item.get("closed_by") or "",
            "closed_at": item.get("closed_at"),
            "reason": _clip(item.get("reason")),
        })
    rows.sort(key=lambda row: (row["status"] != "open", -(int(row["id"]) if
                        isinstance(row["id"], int) else 0)))
    return rows


def _event_rows(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Лента событий: факты без слов о них, словарь живёт в клиенте."""
    rows = []
    for entry in entries:
        # `Store._transcript_entry` не выбрасывает битую строку, а отдаёт её как
        # {_damaged, _raw}, и это правильно: молча пропустить строку значило бы
        # сделать вид, будто события не было. Такая строка становится событием со
        # своим видом, а не фантомом с именем «?».
        if entry.get("_damaged"):
            rows.append({"ts": None, "kind": "damaged", "role": "", "to": "",
                         "summary": _clip(entry.get("_raw"))})
            continue
        rows.append({
            "ts": entry.get("ts"),
            "kind": str(entry.get("kind") or "?"),
            "role": entry.get("role") or "",
            "to": entry.get("to") or "",
            "summary": summarise_event(entry),
        })
    return rows


def _round_section(state: dict[str, Any], event_age: float | None) -> dict[str, Any]:
    phase = str(state.get("phase") or "scoping")
    state_age = _age(state.get("updated_at"))
    return {
        "number": state.get("round", 0),
        "phase": phase,
        "phases": list(PHASES),
        "phase_index": PHASES.index(phase) + 1 if phase in PHASES else 0,
        "mode": state.get("mode") or "fix",
        "autonomy": state.get("autonomy") or "auto",
        "writer": state.get("writer") or "",
        "writer_reason": _clip(state.get("writer_reason")),
        "task": _clip(state.get("task"), TASK_TEXT_CHARS),
        "updated_at": state.get("updated_at"),
        "state_age_seconds": state_age,
        "state_age": human_age(state_age),
        "event_age_seconds": event_age,
        "event_age": human_age(event_age),
        "closed": phase == "done" or bool(state.get("closed_at")),
    }


def build_snapshot(state_dir: Path, *, limit: int = DEFAULT_EVENTS,
                   stale_after: int = DEFAULT_STALE_AFTER) -> dict[str, Any]:
    """Снимок состояния пары. Никогда не бросает на битом состоянии.

    Дашборд открывают, когда что-то сломалось, и «не смог прочитать state.json» —
    худший возможный ответ: вместо картины был бы пустой экран без причины.
    Поэтому каждый разбор прощается с None, а снимок помечает себя неполным.
    """
    state_dir = Path(state_dir)
    store = Store(state_dir)
    read_error = _state_read_error(state_dir)
    try:
        state = store.read()
    except Exception as error:  # noqa: BLE001 - причина показывается, а не проглатывается
        state = store.default_state()
        read_error = read_error or str(error)

    entries = store.read_transcript(limit)
    try:
        total = store.transcript_entry_count()
    except Exception:  # noqa: BLE001 - счётчик — украшение, его отсутствие не повод падать
        total = len(entries)

    tasks = _task_rows(state)
    event_age = _age(entries[-1].get("ts")) if entries else None
    return {
        "generated_at": utcnow(),
        "project": state_dir.parent.name or str(state_dir),
        "state_dir": str(state_dir),
        "read_error": read_error,
        "stale_after": int(stale_after),
        "watch": False,
        "round": _round_section(state, event_age),
        "roles": _role_rows(state),
        "pilot": pilot_section(state_dir, entries),
        "tasks": tasks,
        "events": _event_rows(entries),
        "counts": {"events_shown": len(entries), "events_total": total,
                   "tasks_open": sum(1 for row in tasks if row["status"] == "open"),
                   "roles": len(state.get("roles") or {})},
    }


def snapshot_json(snapshot: dict[str, Any]) -> str:
    """Снимок как строка, безопасная для вставки внутрь `<script>`.

    Закрывающий тег в данных ломает страницу, а данные здесь — текст писем и задач,
    то есть ровно то, что человек писал сам. Поэтому `<`, `>` и `&` уходят в
    escape-последовательности JSON: значение после разбора остаётся тем же самым
    символом, а страница перестаёт быть чем-то, что можно закрыть.
    U+2028 и U+2029 — разделители строк в JS, но не в JSON, наивная вставка ломает
    парсер, и их тоже уводим в escape.
    """
    text = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
    # Обратный слэш экранировать НЕ нужно: `json.dumps` уже выдал корректный JSON.
    # Экранировать его второй раз значило бы превратить каждую экранированную
    # кавычку в `\\\"` и сломать разбор. Добавляются только escape-последовательности,
    # которых в исходном тексте не было, поэтому порядок замен не важен.
    for needle, replacement in (("&", "\\u0026"), ("<", "\\u003c"), (">", "\\u003e"),
                                ("\u2028", "\\u2028"), ("\u2029", "\\u2029")):
        text = text.replace(needle, replacement)
    return text