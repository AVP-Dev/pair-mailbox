"""Чтение и обзор для человека: дайджест, ящик, журнал, заметки, список проектов.

Вынесено из `server.py` отдельной ответственностью: всё здесь только ЧИТАЕТ и
печатает. Ни одна команда этого модуля не меняет состояние пары — запись в
`state.json` есть только у `pairctl` в `pairctl_authority.py`.

Разделение не стилистическое. `server.py` держал 725 строк `pairctl` вперемешку с
протоколом, и команды, которые читают, оказывались в одном файле с командами,
которые пишут. Когда файл читают, чтобы понять, кто сейчас пишет, туда попадал
код, который ничего не пишет.

Что здесь живёт и почему вместе:

- `discover_projects` и `project_row` — это «где вообще идут пары», и на них же
  опирается `bin/pairchat`, который не имеет отношения к `pairctl`. Разносить их
  по двум модулям ради буквы правила значило бы заставить `pairchat` зависеть от
  CLI, который ему не нужен;
- `silence_seconds` и `human_age` — единственное место, где «молчит» превращается
  в текст для человека. Вторая реализация этой арифметики разошлась бы с первой
  так же, как разошёлся бы сам отчёт.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from server import DIGEST_TOKEN_BUDGET, LOG_DEFAULT_LIMIT, Store, build_digest, estimate_tokens


def cmd_digest(store: Store, args: argparse.Namespace) -> int:
    # Тот же текст, что отдаёт `pair_digest`, из одного места: две реализации
    # дайджеста разъедутся, и человек увидит не то, что видит агент.
    text = build_digest(store, args.role, args.budget)
    sys.stdout.write(text)
    used = estimate_tokens(text)
    if used > args.budget:
        # Потолок нарушен — это поломка дайджеста, а не повод сделать вид, что
        # всё в порядке. Пишем в stderr и возвращаем ненулевой код.
        print(f"digest: {used} токенов при потолке {args.budget}", file=sys.stderr)
        return 1
    return 0


def cmd_why(store: Store, args: argparse.Namespace) -> int:
    # Почему сейчас пишет тот, кто пишет. Короткий вопрос, на который агент
    # обязан уметь ответить фактом, а не «ща посмотрю».
    rows = [project_row(root) for root in discover_projects(args.paths)]
    if args.json:
        print(json.dumps([{"project": row["project"], "writer": row["writer"],
                           "reason": row.get("reason", "")} for row in rows],
                         ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print("ни одного проекта с состоянием пары не найдено")
        return 1
    for row in rows:
        print(f"{row['project']}: {row['writer'] or '—'} — {row.get('reason') or 'причина не сказана'}")
    return 0


def cmd_inbox(store: Store, args: argparse.Namespace) -> int:
    role = args.role
    inbox = store.mailbox_root / role
    if not inbox.is_dir():
        print(f"no inbox for {role!r} (they may not have started yet)")
        return 0
    files = sorted(inbox.glob("*.json"))
    print(f"{len(files)} message(s) waiting for {role}")
    for path in files:
        body = json.loads(path.read_text(encoding="utf-8"))
        print(f"\n--- #{body['seq']} {body['ts']} from {body['from']} ---")
        print(body["text"])
    return 0


def cmd_log(store: Store, args: argparse.Namespace) -> int:
    # Дефолтный лимит вместо «всего». Замер: без него команда вываливала в
    # терминал весь активный журнал, а это пол-мегабайта и ~50k токенов на
    # раунд, в том числе у человека, который просто посмотрел, что происходило.
    # Те, кому нужен весь файл, просят об этом явно.
    limit = 0 if args.all else args.limit
    shown = store.read_transcript(limit)
    for entry in shown:
        print(json.dumps(entry, ensure_ascii=False))
    if args.all:
        return 0
    total = store.transcript_entry_count()
    if total > len(shown):
        # Молча выдать 50 строк вместо 900 — это выглядеть как полный журнал.
        print(f"# показаны последние {len(shown)} из {total}; остальное: "
              f"--all или --limit N", file=sys.stderr)
    return 0


def cmd_all(store: Store, args: argparse.Namespace) -> int:
    # Deliberately ignores --state: "all" is exactly the command where one
    # state directory would defeat the point.
    rows = [project_row(root, args.stuck_min) for root in discover_projects(args.paths)]
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print("ни одного проекта с состоянием пары не найдено")
        print("укажите путь:  pairctl all ~/work/foo")
        print("или скажите где искать:  PAIR_SCAN='~/work/*/.pair'")
        return 1
    print(render_table(rows))
    stuck = [row for row in rows if row["stuck"]]
    live = [row for row in rows if not row["closed"]]
    untouched = [row for row in rows if row["silence_seconds"] is None]
    print()
    print(f"{len(rows)} проект(ов), в работе {len(live)}, не начато {len(untouched)}")
    if stuck:
        names = ", ".join(row["project"] for row in stuck)
        print(f"молчат дольше {args.stuck_min} мин: {names}")
        print("  посмотреть:  PAIR_STATE=<путь> bin/pairchat --once")
        return 3
    return 0


def cmd_notes(store: Store, args: argparse.Namespace) -> int:
    for path in sorted(store.notes_root.glob("*.md")):
        print(f"\n===== {path.name} =====")
        print(path.read_text(encoding="utf-8"))
    return 0


def extract_state(argv: list[str]) -> tuple[Path, list[str]]:
    """Pull --state out of argv wherever it appears and resolve it.

    Subparser defaults silently overwrite same-named top-level options in
    argparse, so the option is removed from the grammar entirely and handled
    here instead. Both `server.py --state X ctl status` and
    `server.py ctl status --state X` work.
    """
    # Один резолвер для всех: явный --state выигрывает, затем PAIR_STATE,
    # затем walk-up до .pair/state.json (иначе сервер из вложенного каталога
    # молча создаёт второе независимое состояние — state-dir-not-pinned).
    explicit = [a for a in (os.environ.get("PAIR_STATE") or "").strip().split()]
    raw = explicit[0] if explicit else ""
    rest: list[str] = []
    index = 0
    while index < len(argv):
        item = argv[index]
        if item == "--state" and index + 1 < len(argv):
            raw = argv[index + 1]
            index += 2
            continue
        if item.startswith("--state="):
            raw = item.split("=", 1)[1]
            index += 1
            continue
        rest.append(item)
        index += 1
    resolved = Path(raw).expanduser().resolve() if raw else walk_up_to_state()
    return resolved, rest


def walk_up_to_state(start: Path | None = None) -> Path:
    """Ближайший .pair вверх от cwd — тот же walk-up, что у плагина.

    Без него запуск из подкаталога проекта создаёт второе независимое
    состояние, а плагин при этом показывает корневое: два мнения о писателе
    при одном guard, который существует именно против этого."""
    here = (start or Path.cwd()).resolve()
    for _ in range(12):
        candidate = here / ".pair"
        if (candidate / "state.json").exists():
            return candidate
        parent = here.parent
        if parent == here:
            break
        here = parent
    return (start or Path.cwd()).resolve() / ".pair"


# Where discover_projects looks when the human did not say. PAIR_SCAN overrides it
# with a colon-separated list of globs, so "where should I look" is always something
# the human can answer rather than something we guess at.
DEFAULT_SCAN_GLOBS = ("~/work/*/.pair",)


def discover_projects(extra: list[str] | None = None) -> list[Path]:
    """Every project that actually has pair state, sorted by path.

    A directory only counts when state.json is really inside it. An empty or
    half-created .pair is not a project, and listing it would put a phantom row
    in the human's overview of what is running.

    Naming projects replaces the built-in search. Otherwise `pairchat --all
    ~/work/blog` would show the five projects the scan always finds plus blog,
    and the human who named one project would get six.
    """
    import glob as _glob

    raw = os.environ.get("PAIR_SCAN", "").strip()
    if raw:
        patterns = [p.strip() for p in raw.split(os.pathsep) if p.strip()]
    elif extra:
        patterns = []
    else:
        patterns = list(DEFAULT_SCAN_GLOBS)

    found: dict[str, Path] = {}

    def offer(candidate: Path) -> None:
        candidate = candidate.expanduser()
        # Accept either the .pair directory or the project that contains it, because
        # "pairctl all ~/work/test" is what a human will type.
        if candidate.is_dir() and not (candidate / "state.json").is_file():
            candidate = candidate / ".pair"
        if (candidate / "state.json").is_file():
            resolved = candidate.resolve()
            found[str(resolved)] = resolved

    for pattern in patterns:
        expanded = str(Path(pattern).expanduser()) if pattern.startswith("~") else pattern
        for hit in _glob.glob(expanded, recursive=True):
            offer(Path(hit))
    for given in extra or []:
        offer(Path(given))
    # Never lose the project the human is standing in, whatever PAIR_SCAN says.
    offer(Path.cwd() / ".pair")

    return [found[key] for key in sorted(found)]


def silence_seconds(root: Path) -> float | None:
    """Seconds since the last recorded event, or None when nothing was recorded.

    Taken from the transcript, not from state.json's updated_at: a state write that
    changed nothing would otherwise dress up a stalled pair as busy.
    """
    last: dict | None = None
    try:
        text = (root / "transcript.jsonl").read_text(errors="replace")
    except OSError:
        return None
    for raw in text.splitlines():
        if not raw.strip():
            continue
        try:
            last = json.loads(raw)
        except Exception:
            continue
    if not last or not last.get("ts"):
        return None
    try:
        stamp = datetime.fromisoformat(str(last["ts"]))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - stamp).total_seconds())


def human_age(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 90:
        return f"{int(seconds)} с"
    if seconds < 5400:
        return f"{int(seconds // 60)} мин"
    if seconds < 172800:
        return f"{int(seconds // 3600)} ч"
    return f"{int(seconds // 86400)} д"


def project_row(root: Path, stuck_min: int = 30) -> dict:
    """One project's row for the human's overview. Never raises on a broken file."""
    try:
        state = json.loads((root / "state.json").read_text(encoding="utf-8"))
    except Exception:
        state = {}
    roles = state.get("roles") or {}
    writer = state.get("writer")
    age = silence_seconds(root)
    phase = state.get("phase") or "?"
    closed = bool(state.get("closed_at")) or phase == "done"
    return {
        "path": str(root),
        "project": root.parent.name or str(root),
        "task": (state.get("task") or "").strip(),
        "phase": phase,
        "writer": writer,
        "reason": (state.get("writer_reason") or "").strip(),
        "mode": state.get("mode", "fix"),
        "peers": sorted(name for name in roles if name != writer),
        "roles": sorted(roles),
        "silence_seconds": age,
        "silence": human_age(age),
        "closed": closed,
        "stuck": age is not None and not closed and age > stuck_min * 60,
    }


def render_table(rows: list[dict]) -> str:
    """Aligned plain text. Column widths come from the data, never from a guess."""
    header = ("проект", "фаза", "режим", "писатель", "критик", "тишина", "почему он", "задача")
    table = [
        (
            row["project"],
            row["phase"],
            "AUDIT" if row.get("mode") == "audit" else "fix",
            row["writer"] or "—",
            ", ".join(row["peers"]) or "—",
            "не начат" if row["silence_seconds"] is None
            else row["silence"] + ("  ЗАВИС" if row["stuck"] else ""),
            (row["reason"] or "—")[:40],
            (row["task"] or "—")[:36],
        )
        for row in rows
    ]
    widths = [
        max([len(header[i])] + [len(record[i]) for record in table]) for i in range(len(header))
    ]
    lines = ["  ".join(header[i].ljust(widths[i]) for i in range(len(header))).rstrip()]
    lines.append("  ".join("-" * w for w in widths))
    lines.extend(
        "  ".join(record[i].ljust(widths[i]) for i in range(len(record))).rstrip()
        for record in table
    )
    return "\n".join(lines)