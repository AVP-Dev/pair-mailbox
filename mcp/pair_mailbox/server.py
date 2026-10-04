#!/usr/bin/env python3
"""pair-mailbox: a transport and protocol boundary for two independent coding agents.

Two agents (OpenCode, Antigravity/agy, Codex, Claude Code, anything with an MCP
client) are configured as separate instances of this server, each with its own
``--role``. They share one state directory and use it to talk to each other.

Why this exists: every agent CLI now has multi-agent features, but none of them
can talk to a *different* vendor's CLI. This server is the only channel that
crosses that line, and it is also where the collaboration rules are enforced.

The rules are enforced here, in the server, rather than in prompts. A prompt is
advice; a refusal is a boundary.

Rules
-----
1. Exactly one role holds write authority at a time. Only the human transfers it,
   through ``pairctl``. A peer message can never grant, revoke or move it.
2. A non-writer never edits project files. It reserves a patch file, fills it,
   seals it, and sends the path plus a SHA-256. The writer copies it once and
   re-verifies the hash immediately before applying.
3. A patch is only ever released by exact path. Glob patterns are rejected.
4. "The peer agreed" is not an approval. The server records agreement; only the
   human authorises.
5. Every action lands in an append-only transcript, so a run can be audited
   afterwards.

Transport: MCP over stdio, newline-delimited JSON-RPC 2.0, no dependencies.
Human control surface: ``server.py ctl <command>``.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# composer.py лежит рядом и импортируется лениво, внутри pair_deliver.
# Путь добавляем здесь, после того как Path уже есть.
sys.path.insert(0, str(Path(__file__).resolve().parent))

# MCP protocol revisions this server understands. We echo the client's requested
# revision back when we know it, because clients reject a downgrade they did not
# ask for.
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_PROTOCOL_VERSION = "2024-11-05"

SERVER_NAME = "pair-mailbox"
SERVER_VERSION = "1.0.0"

# Потолок ожидания в pair_read. Живое ожидание — часть протокола (§7): без него
# агент, который не знает о письме, не узнает и не ответит, и канал умирает.
#
# Но сервер не должен обещать больше, чем клиент способен дождаться: opencode
# рвёт MCP-вызов на 20 секундах, и 40 секунд разрешённого диапазона были
# недостижимы. Клиент рвался раньше, чем сервер просыпался, — и по возвращённым
# waited_seconds не было видно, что вызов прервал клиент, а не сервер.
#
# Отсюда два правила. Потолок здесь — последнее слово сервера, а фактический
# предел — min(этот потолок, CLIENT_TIMEOUT с запасом), и он читается из
# окружения, потому что клиент у каждого CLI свой. И проверка согласованности
# при старте: профиль с таймаутом меньше потолка — это дефект конфигурации, и
# молча пропускать его нельзя.
MAX_READ_WAIT_SECONDS = 60
DEFAULT_READ_WAIT_SECONDS = 0.0

# Таймаут MCP-клиента по умолчанию, если он не задан в окружении. 20 секунд —
# значение из коробки у opencode, и оно же было причиной находки.
CLIENT_TIMEOUT_SECONDS = int(os.environ.get("PAIR_CLIENT_TIMEOUT", "20000"))

# Запас между ожиданием сервера и терпением клиента. Без него сервер успевает
# начать ответ ровно в момент, когда клиент уже отключился.
READ_WAIT_MARGIN_SECONDS = 5.0


def effective_read_wait(wait: float) -> tuple[float, bool]:
    """Сколько реально ждать, и что пришлось урезать.

    Возвращает (ожидание, урезано ли). Урезанное ожидание возвращается наружу
    явно, потому что иначе агент видит «я попросил 60 и получил тишину» и не
    может отличить «не ответили» от «меня отключили по таймауту».
    """
    capped = min(float(wait), float(MAX_READ_WAIT_SECONDS))
    client_budget = max(0.0, (CLIENT_TIMEOUT_SECONDS / 1000.0) - READ_WAIT_MARGIN_SECONDS)
    effective = min(capped, client_budget)
    return effective, effective < capped
GLOB_CHARS = ("*", "?", "[", "]")

# Phase graph. Mirrors the shape that works in practice: scope the work, agree on
# a plan, write it, then verify against the real code before calling it done.
PHASES = ("scoping", "plan", "implement", "verify", "done")
PHASE_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "scoping": ("plan",),
    "plan": ("scoping", "implement"),
    "implement": ("plan", "verify"),
    "verify": ("implement", "done"),
    "done": ("implement",),
}

# One step back belongs to any role, not only the writer. This is the critic's
# veto: a round cannot be closed on top of a finding nobody reported yet, and
# reopening a round is not a write operation.
PHASE_BACKWARD: dict[str, str] = {
    "plan": "scoping",
    "implement": "plan",
    "verify": "implement",
    "done": "implement",
}

# Round modes. `fix` is the default and everything that existed before: the writer
# edits project files, the critic proposes sealed patches, git ends in a PR.
#
# `audit` is a round where nothing is changed on purpose. The pair still does real
# work — find defects, try to refute each other's findings, park what will not be
# fixed — but the deliverable is evidence, not a diff. The distinction lives in
# shared state rather than in the task text, because every agent gets its standing
# instructions from state and prompt, and those two must not contradict the task.
MODES = ("fix", "audit")

# How much the pair runs without the human. This is deliberately a SEPARATE axis
# from `mode`: mode says what a round is for, autonomy says how far it may go on
# its own. Folding them together would make `pairctl mode auto` silently turn an
# audit round back into a code round, because every audit check tests `mode ==
# "audit"`.
#
# - observe: the supervisor only speaks. It does not restart a dead agent and
#   does not confirm a permission prompt. The human is at the wheel.
# - semi (default): the supervisor restarts a dead agent and confirms prompts on
#   the allowlist, and closing a round needs evidence plus one word from the human.
# - auto: the same, and the round may close itself once the evidence is there.
#
# §12 is outside this axis entirely: secrets, ports, deploys, migrations, deleting
# data or git history, and production stop the pair in every mode.
AUTONOMIES = ("observe", "semi", "auto")

# What may be recorded as proof that the round did something. Every kind here is
# something a machine can produce on its own; "the peer seemed happy" is not one
# of them, which is the whole point.
EVIDENCE_KINDS = ("test", "lint", "typecheck", "build", "diff", "commit", "pr", "manual")

EVIDENCE_KEEP = 50

# Фазы, на которых слово человека «ок» что-то значит. Раньше проверялся только
# номер раунда, и одобрение плана закрывало раунд на verify.
OK_PHASES = ("verify", "done")

# Auto-release is off unless the human turns it on. A writer that has finished
# and gone quiet holds authority in no useful sense.
STALL_SECONDS = int(os.environ.get("PAIR_STALL_SECONDS", "0") or 0)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(message: str) -> None:
    """Diagnostics go to stderr. stdout carries the protocol and nothing else."""
    print(f"[{SERVER_NAME}] {message}", file=sys.stderr, flush=True)


def global_roles_path() -> Path:
    """Where the human's across-every-project role pinning lives."""
    return Path(os.environ.get("PAIR_ROLES", Path.home() / ".config" / "pair-mailbox" / "roles.json"))


def permissions_path(root: Path) -> Path:
    """Where the human keeps the list of prompts a supervisor may confirm."""
    return Path(os.environ.get("PAIR_PERMISSIONS", root / "permissions.json"))


def load_allowlist(root: Path) -> list[str]:
    """Разрешённые шаблоны и признак, что мы на списке по умолчанию.

    Раньше отсутствующий файл означал пустой список, то есть «не подтверждать
    ничего», и сверхвизор не подтверждал вообще ничего — при этом снаружи это
    выглядело как молчание соседа, а не как отказ по настройке. Граница задана
    человеком: внутри проекта можно всё, что нужно для работы, запрещено то, что
    вредит системе или чужому проекту. Поэтому файла нет = список по умолчанию.

    Сломанный файл — другое дело: оператор что-то настраивал, и молча подставлять
    дефолт вместо его настройки хуже, чем остановиться.
    """
    from composer import load_allow_patterns

    path = permissions_path(root)
    try:
        is_file = path.is_file()
    except OSError:
        is_file = False
    if not is_file:
        from composer import DEFAULT_ALLOW_PATTERNS
        return list(DEFAULT_ALLOW_PATTERNS), True
    try:
        return load_allow_patterns(str(path))
    except Exception:  # noqa: BLE001 - сломанная настройка не должна ронять сервер
        return [], False


def autonomy_of(state: dict[str, Any]) -> str:
    """Read the autonomy level, refusing to invent one.

    An unknown value is treated as `observe`, the level that does the least: a
    state written by a newer build must never hand this build more authority than
    the human asked for.
    """
    value = state.get("autonomy", "semi")
    return value if value in AUTONOMIES else "observe"


# The three helpers below are plain functions of the state, not methods, because
# two processes need to agree on them: the server decides whether a round may
# close, and bin/pairpilot has to tell the human the same thing in the same words.
# A second copy of this rule would eventually disagree with the first, and the
# human would be shown "all good" by one side and refused by the other.


def round_evidence(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Evidence recorded for the round that is running right now.

    Scoped to the round on purpose. Evidence is not a trophy the pair keeps: what
    proved round 3 says nothing about round 7, and carrying it forward would let a
    later round be closed by an earlier round's tests.
    """
    current = int(state.get("round", 0))
    return [item for item in (state.get("evidence") or []) if item.get("round") == current]


OPEN_TASK = "open"
TASK_STATUSES = (OPEN_TASK, "done", "dropped")


def tasks_of(state: dict[str, Any]) -> list[dict[str, Any]]:
    return list(state.get("tasks") or [])


def open_tasks(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in tasks_of(state) if item.get("status") == OPEN_TASK]


def next_task_id(state: dict[str, Any]) -> int:
    return max((int(item.get("id", 0)) for item in tasks_of(state)), default=0) + 1


def human_ok_for_round(state: dict[str, Any]) -> bool:
    """Слово человека на чекпоинте — и только на том, где оно значимо.

    Раньше проверялся только номер раунда, поэтому «ок: план одобряю» на фазе
    plan закрывало раунд на verify. План и «правки проверены» — разные
    утверждения, а гейт их не различал.

    Теперь засчитывается только ok, сказанное на verify или done, и только пока
    раунд на этой фазе. Откат фазы назад отменяет слово само собой: approve был
    привязан к тому, что человек видел.
    """
    current = int(state.get("round", 0))
    if state.get("phase") not in OK_PHASES:
        # Раунд откатили назад — сказанное на verify относилось к тому, что
        # человек тогда видел, и после отката он этого уже не видел.
        return False
    entries = [entry for entry in (state.get("human_ok") or [])
               if int(entry.get("round", -1)) == current]
    if not entries:
        return False
    # Последнее слово решает: отзыв отменяет более раннее одобрение, а повторное
    # «ок» после отзыва — новое слово.
    last = entries[-1]
    if last.get("revoke"):
        return False
    # И сказано оно должно быть на чекпоинте, где что-то значит: «план одобрен»
    # и «правки проверены» — разные утверждения.
    return last.get("phase") in OK_PHASES


def unresolved_patches(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Sealed patches that nobody has recorded as applied or released.

    `applied` is set to False at reserve and flipped only by `pair_patch_applied`.
    Before that verb existed there was nowhere to say whether a patch had reached
    the tree, so a round could close over a proposal that had been sitting sealed
    and untouched since the night before. That is the exact shape of work that
    looks finished and is not. A failed apply leaves `applied` False on purpose:
    a rejected hunk is not an answer, it is an open question.
    """
    out = []
    for path, record in (state.get("patches") or {}).items():
        if not record.get("sealed") or record.get("applied"):
            continue
        out.append({"path": path, "owner": record.get("owner"),
                    "sha256": record.get("sha256"),
                    "sealed_at": record.get("sealed_at"),
                    "verified_match": record.get("verified_match")})
    return out


def done_blockers(state: dict[str, Any]) -> list[str]:
    """Everything that must be true before a round may close, in plain words.

    The refusal message is the only thing the writer sees at that moment, so it
    has to say what is missing and what to call. A gate nobody can satisfy is
    indistinguishable from a broken server.
    """
    blockers: list[str] = []
    autonomy = autonomy_of(state)
    round_number = int(state.get("round", 0))

    # Per kind, the LAST run decides, not any of them: tests failed, got fixed,
    # ran again — the round may close. Otherwise the first failure would hold the
    # round open forever, because the journal only grows.
    latest: dict[Any, dict[str, Any]] = {}
    for item in round_evidence(state):
        latest[item.get("kind")] = item
    failed = [item for item in latest.values() if not item.get("ok")]
    passed = [item for item in latest.values() if item.get("ok")]
    if failed:
        names = ", ".join(f"{item.get('kind')}={item.get('value')!r}" for item in failed)
        blockers.append(
            f"round {round_number} ends on failing evidence ({names}). Fix it and record the "
            f"passing result, or say in a note why the failure does not block closing."
        )
    if not passed:
        blockers.append(
            f"round {round_number} has no passing evidence. Call pair_evidence with what you "
            f"actually ran: tests with the count and exit code, lint, typecheck, or the commit "
            f"and PR. Kinds: {', '.join(EVIDENCE_KINDS)}."
        )

    if state.get("mode", "fix") != "audit" and not any(
            item.get("kind") == "test" and item.get("ok")
            and item.get("role") == state.get("writer")
            for item in latest.values()):
        # Раньше гейт спрашивал «есть ли хоть одна успешная запись», и на этот
        # вопрос отвечал `kind="manual", value="looks good"`. Запись, неотличимая
        # от прогона, — это ровно то, чем гейт не должен закрываться: раунд с
        # красными тестами объявлялся закрытым, потому что автор описал словами,
        # что всё хорошо. В mode=audit требования нет — там результат работы это
        # находки в заметках, и прогона тестов может не быть по существу.
        #
        # И прогон должен быть писательский. Иначе гейт, который защищает от
        # «закрыть на словах», закрывается на словах же — просто от другой роли:
        # критик может записать ok=True и удовлетворить гейт человека, который
        # право писать не имеет.
        writer = state.get("writer")
        blockers.append(
            f"round {round_number} has no passing test run from the writer "
            f"({writer or 'nobody holds authority'}), and mode=fix closes only on one. "
            f"The writer calls pair_evidence(kind=\"test\", value=\"181 passed, exit 0\") "
            f"with what the machine actually printed. kind=lint, typecheck or a commit hash "
            f"is not a substitute, another role's 'ok' is not a substitute either, and neither "
            f"is a verdict that things look right."
        )

    if autonomy == "semi" and not human_ok_for_round(state):
        blockers.append(
            "autonomy is semi, so closing a round waits for the human. Send them the digest "
            "and, once they agree, run `bin/pairctl ok`. `bin/pairctl autonomy auto` closes "
            "without that."
        )

    for item in open_tasks(state):
        blockers.append(
            f"task #{item.get('id')} is still open: {str(item.get('text'))[:100]!r}. "
            f"A round closes only when nothing is left open: pair_task(action=\"done\", id={item.get('id')}) "
            f"with it finished, or pair_task(action=\"drop\", id={item.get('id')}, "
            f"reason=\"why not\") if it turned out not to be needed. Dropping needs a "
            f"reason on purpose — a task vanishes silently otherwise, which is the "
            f"failure this list exists to prevent."
        )

    if state.get("mode", "fix") != "audit":
        for item in unresolved_patches(state):
            blockers.append(
                f"patch {item['path']} (sha256 {item['sha256']}) was sealed by {item['owner']} "
                f"and never applied. Verify it with pair_patch_verify, then "
                f"pair_patch_applied(path, ok), or pair_patch_release if it was rejected."
            )
    return blockers


def render_tasks_md(state: dict[str, Any]) -> str:
    """Журнал сессии: что задано, что сделано, чем доказано, что НЕ сделано.

    Генерируется из состояния, а не пишется руками, поэтому не устаревает и не
    может разойтись с фактами. Живёт в .pair, а не в docs: это рабочий журнал
    пары, а не документация проекта, и коммитить его в публичный репозиторий
    незачем.

    Секция «не сделано» не может быть молча пустой: у открытой задачи она
    заполнена автоматически, а закрытая обязана нести причину, если она брошена.
    Именно это и нужно человеку, который вернулся через неделю.
    """
    items = tasks_of(state)
    mark = {OPEN_TASK: "НЕ СДЕЛАНО", "done": "сделано", "dropped": "брошено"}
    lines = [
        "# Журнал сессии",
        "",
        f"- раунд: {state.get('round')}   фаза: {state.get('phase')}   "
        f"режим: {state.get('mode')}   автономность: {autonomy_of(state)}",
        f"- писатель: {state.get('writer') or 'не назначен'}",
        f"- сгенерировано: {utcnow()}",
        "",
    ]
    if not items:
        lines += ["Задач нет. `pairctl task \"...\"` или pair_task(action=\"add\").", ""]
        return "\n".join(lines)

    still_open = [i for i in items if i.get("status") == OPEN_TASK]
    lines += [
        "## Сводка",
        "",
        f"- всего задач: {len(items)}",
        f"- не сделано: {len(still_open)}"
        + (f" — #{', #'.join(str(i['id']) for i in still_open)}" if still_open else ""),
        "",
        "## Задачи",
        "",
    ]
    for item in items:
        ident = item.get("id")
        lines.append(f"### #{ident} — {mark.get(item.get('status'), item.get('status'))}")
        lines.append("")
        lines.append(str(item.get("text", "")).strip())
        lines.append("")
        lines.append(f"- добавил: {item.get('added_by')}   раунд: {item.get('round')}   "
                     f"в {item.get('added_at')}")
        if item.get("status") != OPEN_TASK:
            lines.append(f"- закрыл: {item.get('closed_by')}   в {item.get('closed_at')}")
        if item.get("status") == "dropped":
            reason = str(item.get("reason") or "").strip()
            lines.append(f"- почему брошено: {reason or 'ПРИЧИНА НЕ УКАЗАНА — это дефект'}")
        proved = [e for e in (state.get("evidence") or []) if e.get("task") == ident]
        if proved:
            lines.append("- чем доказано:")
            for entry in proved:
                flag = "OK " if entry.get("ok") else "FAIL"
                lines.append(f"    - {flag} {entry.get('kind')} [{entry.get('role')}] "
                             f"{entry.get('value')}")
        elif item.get("status") != OPEN_TASK:
            lines.append("- чем доказано: НИЧЕМ. Закрыто без единого машинного факта.")
        else:
            lines.append("- чем доказано: ещё нет, работа не закончена.")
        notes = [e for e in (state.get("evidence") or [])
                 if e.get("task") == ident and not e.get("ok")]
        if notes:
            lines.append("- что падало:")
            for entry in notes:
                lines.append(f"    - {entry.get('value')}")
        lines.append("")

    if still_open:
        lines += [
            "## Что осталось за открытыми задачами",
            "",
            "Раунд не закроется, пока это не сделано или не брошено с причиной.",
            "",
        ]
        for item in still_open:
            lines.append(f"- #{item.get('id')}: {str(item.get('text'))[:120]}")
        lines.append("")

    loose = [e for e in (state.get("evidence") or []) if not e.get("task")]
    if loose:
        lines += [
            "## Доказательства без привязки к задаче",
            "",
            "Записаны, когда открытых задач было больше одной, поэтому не отнесены "
            "ни к какой. К задаче привязывается явно: pair_evidence(..., task=N).",
            "",
        ]
        for entry in loose:
            flag = "OK " if entry.get("ok") else "FAIL"
            lines.append(f"- {flag} {entry.get('kind')} [{entry.get('role')}] {entry.get('value')} "
                         f"— открыты были: {', '.join(f'#{i}' for i in entry.get('tasks_open') or []) or '—'}")
        lines.append("")
    return "\n".join(lines)


def write_tasks_md(state_dir: Path, state: dict[str, Any]) -> Path:
    target = Path(state_dir) / "tasks.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_tasks_md(state), encoding="utf-8")
    return target


def read_global_policy() -> dict[str, Any] | None:
    """The pinning the human set for all projects at once.

    Read on every state load and only when the project has none of its own, so a
    project can always override it and never the other way round. An unreadable or
    nameless file means no policy, which leaves the race in place rather than
    guessing — a wrong writer is far worse than a slow one.
    """
    try:
        data = json.loads(global_roles_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(data, dict) and data.get("writer"):
        return data
    return None


class ProtocolError(Exception):
    """A request that is well-formed MCP but violates the collaboration rules."""

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


class Store:
    """Filesystem-backed shared state, guarded by an advisory lock.

    Two server processes with different roles share this directory, so every
    read-modify-write goes through the lock. Writes are atomic renames.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.mailbox_root = root / "mailbox"
        self.notes_root = root / "notes"
        self.patches_root = root / "patches"
        self.obligations_root = root / "obligations"
        self.state_path = root / "state.json"
        self.transcript_path = root / "transcript.jsonl"
        self.lock_path = root / ".lock"
        for directory in (self.root, self.mailbox_root, self.notes_root,
                          self.patches_root, self.obligations_root):
            directory.mkdir(parents=True, exist_ok=True)

    def default_state(self) -> dict[str, Any]:
        return {
            "task": "",
            "mode": "fix",
            "autonomy": "semi",
            "writer": None,
            "phase": "scoping",
            "round": 0,
            "created_at": utcnow(),
            "updated_at": utcnow(),
            "roles": {},
            "seen": {},
            "patches": {},
            "evidence": [],
            "human_ok": [],
            "tasks": [],
        }

    def _load(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return self.default_state()
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            log(f"state.json unreadable ({error}); starting from defaults")
            try:
                self.append_transcript("state_corrupt", "server", error=str(error))
            except Exception:
                pass
            return self.default_state()
        state = self.default_state()
        state.update(data)
        # Старые состояния держали одну строку task. Переносим её в список, чтобы
        # текст задачи не исчез: дальше он дописывается, а не перезаписывается.
        # Мигрируем только если ключа tasks нет вовсе — то есть состояние
        # правда старше списка. Явный пустой список значит «уже перешли на
        # новый язык», и поднимать из него задачу снова нельзя.
        legacy = str(state.get("task") or "").strip()
        if legacy and "tasks" not in data:
            state["tasks"] = [{
                "id": 1, "text": legacy, "status": "open", "added_at": state.get("created_at"),
                "added_by": "human", "round": int(state.get("round", 0)),
                "reason": "", "closed_at": None, "closed_by": None,
            }]
        state.setdefault("tasks", [])
        return state

    def _save(self, state: dict[str, Any]) -> None:
        state["updated_at"] = utcnow()
        atomic_write_json(self.state_path, state)

    class _Lock:
        _holders: dict[Path, tuple[int, Any]] = {}

        def __init__(self, path: Path) -> None:
            self.path = path.resolve()

        def __enter__(self):
            entry = Store._Lock._holders.get(self.path)
            if entry is None or entry[0] == 0:
                handle = self.path.open("a+")
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                Store._Lock._holders[self.path] = (1, handle)
            else:
                depth, handle = entry
                Store._Lock._holders[self.path] = (depth + 1, handle)
            return self

        def __exit__(self, *exc):
            entry = Store._Lock._holders.get(self.path)
            assert entry is not None and entry[0] > 0
            depth, handle = entry
            if depth == 1:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()
                del Store._Lock._holders[self.path]
            else:
                Store._Lock._holders[self.path] = (depth - 1, handle)
            return False

    def lock(self) -> "Store._Lock":
        return Store._Lock(self.lock_path)

    def read(self) -> dict[str, Any]:
        with self.lock():
            return self._load()

    def mutate(self, fn) -> dict[str, Any]:
        """Run fn(state) under the lock and persist the result."""
        with self.lock():
            state = self._load()
            result = fn(state)
            self._save(state)
            return result

    def append_transcript(self, kind: str, role: str, **fields: Any) -> None:
        entry = {"ts": utcnow(), "kind": kind, "role": role}
        entry.update(fields)
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with self.lock():
            with self.transcript_path.open("a", encoding="utf-8") as handle:
                handle.write(line)

    def read_transcript(self, limit: int = 0) -> list[dict[str, Any]]:
        if not self.transcript_path.exists():
            return []
        entries: list[dict[str, Any]] = []
        with self.transcript_path.open("r", encoding="utf-8") as handle:
            for raw in handle:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entries.append(json.loads(raw))
                except json.JSONDecodeError:
                    continue
        return entries[-limit:] if limit else entries


def atomic_write_json(path: Path, payload: Any) -> None:
    handle, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def is_pid_alive(pid: Any) -> bool:
    """True if pid is an integer of a currently running process."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError as err:
        return err.errno == errno.EPERM


def pane_exists(pane: str) -> bool:
    """True if tmux still has this pane id.

    A pane id alone proves nothing across a reboot or a session change: %1 is a
    short-lived name that a fresh session reuses for an unrelated window. That is
    why clearing a stale pane needs this check and not just a dead pid.

    False whenever tmux is absent — the honest answer is "cannot vouch for it",
    and the caller is deciding whether to drop a record, so failing closed keeps
    the record rather than guessing.
    """
    if not pane or not re.fullmatch(r"%[0-9]+", str(pane)):
        return False
    try:
        tmux = shutil.which("tmux")
        if not tmux:
            return False
        result = subprocess.run([tmux, "display-message", "-p", "-t", str(pane), "#{pane_dead}"],
                                capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    # Несуществующая панель — это НЕ «жива». tmux на такой запрос отвечает
    # кодом 0 и ПУСТЫМ выводом, так что проверка только на код успеха и на
    # "не 1" объявляла бы живой любую фантомную панель — ровно тот случай, от
    # которого мы чистим запись.
    return result.returncode == 0 and result.stdout.strip() in ("0", "false")


class PairSession:
    """One agent's view of the shared state, bound to a role at construction."""

    def __init__(self, role: str, store: Store) -> None:
        if not role or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", role):
            raise SystemExit(f"invalid --role {role!r}: use letters, digits, dot, dash, underscore")
        self.role = role
        self.store = store

    # ---------------------------------------------------------------- helpers

    def _register(self, state: dict[str, Any]) -> None:
        roles = state.setdefault("roles", {})
        pane = os.environ.get("TMUX_PANE")
        current_pid = os.getpid()

        # If this role already existed with a different living PID, record collision
        existing = roles.get(self.role)
        if existing and existing.get("pid") and existing["pid"] != current_pid:
            if is_pid_alive(existing["pid"]):
                self.store.append_transcript(
                    "role_collision",
                    self.role,
                    current_pid=current_pid,
                    existing_pid=existing["pid"],
                )

        # Another role claims this pane. Clearing its pane on a dead pid alone cost
        # us a night: the panel was alive, its process belonged to a role that had
        # merely restarted, so delivery to a live peer failed with "has no tmux
        # pane" while the peer sat two windows away, waiting.
        #
        # So a pane goes away only on two independent facts: the process is gone
        # AND tmux no longer has the panel. After a reboot or a session change the
        # panel is absent too, which is what makes this correct in the other
        # direction — never clearing would leave a phantom %1 pointing at whatever
        # window now owns that id.
        if pane:
            for other_name, other_info in list(roles.items()):
                if other_name != self.role and other_info.get("pane") == pane:
                    other_pid = other_info.get("pid")
                    if other_pid and not is_pid_alive(other_pid) and not pane_exists(other_info["pane"]):
                        other_info.pop("pane", None)
                        self.store.append_transcript(
                            "pane_stale_cleared", self.role, pane=pane,
                            other_role=other_name, other_pid=other_pid,
                        )
                    elif other_pid:
                        self.store.append_transcript(
                            "pane_collision",
                            self.role,
                            pane=pane,
                            other_role=other_name,
                            other_pid=other_pid,
                            other_alive=is_pid_alive(other_pid),
                        )

        entry = roles.setdefault(self.role, {"first_seen": utcnow(), "reads": 0, "sends": 0})
        entry["last_seen"] = utcnow()
        entry["pid"] = current_pid
        if pane:
            entry["pane"] = pane

    def _require_not_writer(self, action: str) -> None:
        state = self.store.read()
        if state.get("writer") == self.role:
            raise ProtocolError(
                f"{self.role} holds write authority, so it must not {action}.",
                "The writer edits project files directly. Only a non-writer proposes patches.",
            )

    def _require_fix_mode(self, action: str) -> None:
        """Refuse an action that only makes sense in a round that changes code.

        An audit round has no diff by definition, so a sealed patch in one is either
        the wrong round or an agent working from stale instructions. Either way the
        cheap answer is a refusal that names the mode, and an audit record of it.
        """
        state = self.store.read()
        if state.get("mode", "fix") == "audit":
            raise ProtocolError(
                f"this is an audit round (mode=audit), so there is nothing to {action}.",
                "Findings go to pair_note with kind 'finding', confirmed findings become "
                "docs/backlog entries via the writer. Use `bin/pairctl mode fix` to change "
                "the mode back.",
            )

    def _known_roles(self, state: dict[str, Any]) -> list[str]:
        return sorted(state.get("roles", {}))

    def _policy(self, state: dict[str, Any]) -> dict[str, Any]:
        """Постоянная расстановка ролей, заданная человеком.

        Без неё каждый раунд начинается гонкой, а гонка — это время впустую и
        повод остановиться посреди работы. С ней всё известно заранее: один
        всегда пишет, второй всегда ревьюит, и ни один не тратит ход на спор.

        Это решение человека, поэтому оно и хранится как решение человека, а не
        как побочный эффект pair_claim.
        """
        policy = state.get("policy")
        if isinstance(policy, dict) and policy.get("writer"):
            return policy
        # Глобальная расстановка действует, только если её писатель РЕАЛЬНО
        # работает в этом проекте. Иначе она вредна: назначенной роли здесь нет,
        # она не появится никогда, и право записи не возьмёт уже никто — работа
        # встанет с причиной, которую никто не формулирует.
        roles = state.get("roles") or {}
        if roles:
            inherited = read_global_policy()
            if inherited and inherited.get("writer") in roles:
                return inherited
        return {}

    # ------------------------------------------------------------------ tools

    def tool_pair_state(self) -> dict[str, Any]:
        """Read the shared state, the roster, and this role's standing."""
        with self.store.lock():
            state = self.store._load()
            self._register(state)
            self.store._save(state)

        writer = state.get("writer")
        policy_writer = self._policy(state).get("writer")
        # `_load` merges whatever was on disk over the defaults, so a hand-edited or
        # newer-server state can carry a mode this build does not know. Reading it as a
        # fix round is the safe direction: an unrecognised value must never be a licence
        # to edit, and must never be able to crash an agent's first pair_state call.
        mode = state.get("mode", "fix")
        if mode not in MODES:
            mode = "fix"
        autonomy = autonomy_of(state)
        current_phase = state.get("phase", "scoping")
        you_are_writer = writer == self.role
        if you_are_writer:
            backward = PHASE_BACKWARD.get(current_phase)
            forward = PHASE_TRANSITIONS.get(current_phase, ())
            legal_next = list(forward)
            if backward and backward not in legal_next:
                legal_next.append(backward)
        else:
            backward = PHASE_BACKWARD.get(current_phase)
            legal_next = [backward] if backward else []

        return {
            "you": self.role,
            "you_are_writer": you_are_writer,
            "task": state.get("task", ""),
            "mode": mode,
            # What the mode means for this role, stated here because it decides what an
            # agent is supposed to do and the standing instructions are written for the
            # default. An audit round changes no project file, so a writer's job in one
            # is evidence, not a diff.
            "mode_meaning": {
                "fix": (
                    "Code gets changed: the writer edits project files, the critic proposes "
                    "sealed patches, git ends in a branch and a PR."
                ),
                "audit": (
                    "Nothing in the project gets changed on purpose. Do not edit project "
                    "files. Findings go to pair_note; each one is checked by the peer, "
                    "confirmed ones become docs/backlog entries, and the deliverable is "
                    "evidence rather than a diff."
                ),
            }[mode],
            "writer": writer,
            "autonomy": autonomy,
            "autonomy_meaning": {
                "observe": (
                    "The human is driving. The supervisor speaks but does not act: it will not "
                    "restart a dead agent and will not confirm a permission prompt."
                ),
                "semi": (
                    "The pair works unattended, but closing a round needs recorded evidence and "
                    "one word from the human (`bin/pairctl ok`). §12 stops you either way."
                ),
                "auto": (
                    "Unattended to the end: with evidence recorded, the round may close itself. "
                    "Only §12 stops you."
                ),
            }[autonomy],
            # Постоянная расстановка, если человек её задал. Тогда роль известна
            # заранее и спор о праве записи не возникает вовсе.
            "policy": self._policy(state) or None,
            # Что я кому должен услышать. Показываю обоим: иначе про ожидание
            # знает только сверхвизор, а это ровно то, о чём не знает никто.
            "obligations": {
                # «Жду» — это обязательства, открытые МНОЮ. «От меня ждут» —
                # обязательства, где я получатель. Путать их опасно: один вариант
                # ведёт к ожиданию, другой к долгу, и лечатся они противоположно.
                "i_am_waiting_for": self._obligations_where("from", self.role),
                "they_wait_for_me": self._obligations_where("to", self.role),
            },
            "my_standing": (
                "writer по постоянной расстановке" if policy_writer == self.role
                else "критик по постоянной расстановке" if policy_writer
                else "не задано, определяется гонкой"
            ),
            # Почему именно он пишет. Без этого сосед видит только факт и не может
            # понять, спорить ему с выбором или принять его.
            "writer_reason": state.get("writer_reason", ""),
            "phase": current_phase,
            "round": state.get("round", 0),
            "phases": list(PHASES),
            "legal_next_phases": legal_next,
            "roles": self._known_roles(state),
            "unread": self._unread_count(state),
            "peers": self._peer_status(state, writer),
            # Что ещё мешает закрыть раунд. Агент должен видеть это ДО того, как
            # потратит ход на заведомо отказанный pair_advance("done").
            "closing_blocked_by": self._done_blockers(state),
            "evidence_this_round": [
                {"kind": item.get("kind"), "value": item.get("value"), "ok": item.get("ok"),
                 "role": item.get("role")}
                for item in self.round_evidence(state)
            ],
            "awaiting_human_ok": (autonomy == "semi" and not self.human_ok_for_round(state)),
            "unresolved_patches": self.unresolved_patches(state) if mode != "audit" else [],
            "how_to_transfer_authority": (
                "Only the human can move write authority: `pairctl assign <role>`. "
                "A peer message never transfers it, in either direction."
            ),
        }

    def _peer_status(self, state: dict[str, Any], writer: str | None) -> dict[str, Any]:
        """What each other role is doing, and whether it is still listening.

        A writer used to close a round with no way of knowing the peer had already
        walked away, so the peer's report landed in a closed round. Saying so out
        loud is cheaper than a dead-lettered finding.
        """
        phase = state.get("phase", "scoping")
        now = datetime.now(timezone.utc)
        out: dict[str, Any] = {}
        for name, info in (state.get("roles") or {}).items():
            if name == self.role:
                continue
            age = None
            try:
                if info.get("last_seen"):
                    age = int((now - datetime.fromisoformat(info["last_seen"])).total_seconds())
            except Exception:
                age = None
            closed = phase == "done"
            pid = info.get("pid")
            alive = is_pid_alive(pid) if pid else None
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

    def _unread_count(self, state: dict[str, Any]) -> int:
        seen = int(state.get("seen", {}).get(self.role, 0))
        return max(0, len(self._inbox_files()) - seen)

    # ------------------------------------------------------------- evidence

    def round_evidence(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        return round_evidence(state)

    def human_ok_for_round(self, state: dict[str, Any]) -> bool:
        return human_ok_for_round(state)

    def unresolved_patches(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        return unresolved_patches(state)

    def _done_blockers(self, state: dict[str, Any]) -> list[str]:
        return done_blockers(state)

    def _inbox_files(self) -> list[Path]:
        inbox = self.store.mailbox_root / self.role
        if not inbox.is_dir():
            return []
        return sorted(inbox.glob("*.json"), key=lambda p: p.name)

    def _writer_is_abandoned(self, state: dict[str, Any], writer: str) -> bool:
        """True when the holder closed the round and has gone quiet long enough
        that it no longer holds authority in any useful sense.

        Off by default. Enable with PAIR_STALL_SECONDS=1800 in the environment of
        both MCP servers. Requires a closed round deliberately: a writer pausing to
        run a long suite mid-task must never lose the tree.
        """
        if STALL_SECONDS <= 0:
            return False
        if state.get("phase") != "done":
            return False
        last_seen = (state.get("roles", {}).get(writer) or {}).get("last_seen")
        if not last_seen:
            return False
        try:
            silent = (datetime.now(timezone.utc) - datetime.fromisoformat(last_seen)).total_seconds()
        except Exception:
            return False
        return silent >= STALL_SECONDS

    def tool_pair_claim(self, reason: str = "") -> dict[str, Any]:
        """Take write authority. Refused only while someone else actively holds it.

        `reason` is optional but meant to be used. Whoever reads the task first knows
        more than whoever merely got there first, so saying why is what turns a race
        into a choice. It goes into the transcript and into the refusal message, so
        the loser can see the argument and decide whether to contest it.
        """
        def apply(state: dict[str, Any]) -> dict[str, Any]:
            self._register(state)
            policy = self._policy(state)
            designated = policy.get("writer")
            if designated and designated != self.role:
                raise ProtocolError(
                    f"по постоянной расстановке ролей писатель {designated!r}, а ты критик.",
                    "Это решение человека, а не гонка: работай критиком и не трать ход на спор. "
                    "Сменить расстановку может только человек: `pairctl roles --swap`.",
                )
            if designated and designated == self.role and not state.get("writer"):
                state["writer"] = self.role
                state["writer_reason"] = "назначено политикой ролей"
                return {
                    "already_writer": False,
                    "writer": self.role,
                    "round": state.get("round", 0),
                    "by_policy": True,
                }
            current = state.get("writer")
            was_closed = bool(state.get("closed_at") or state.get("phase") == "done")
            if current and current != self.role:
                if not self._writer_is_abandoned(state, current) and not was_closed:
                    held = str(state.get("writer_reason") or "").strip()
                    raise ProtocolError(
                        f"write authority is held by {current!r}"
                        + (f", which claimed it because: {held}" if held else "."),
                        "Ask the human: `pairctl release` or `pairctl assign <role>`. "
                        "If you would rather write, say so in pair_send and let the "
                        "current writer release it with pair_release.",
                    )
                state["writer"] = self.role
                state["round"] = int(state.get("round", 0)) + 1
                if reason.strip():
                    state["writer_reason"] = reason.strip()
                if was_closed:
                    state.pop("closed_at", None)
                    state["phase"] = "implement"
                return {
                    "already_writer": False,
                    "writer": self.role,
                    "round": state["round"],
                    "took_over": True,
                }
            if current == self.role:
                if reason.strip():
                    state["writer_reason"] = reason.strip()
                if was_closed:
                    state.pop("closed_at", None)
                    state["phase"] = "implement"
                    state["round"] = int(state.get("round", 0)) + 1
                    return {"already_writer": False, "writer": self.role, "round": state["round"], "took_over": False}
                return {"already_writer": True, "writer": self.role, "round": int(state.get("round", 0)), "took_over": False}
            state["writer"] = self.role
            state["round"] = int(state.get("round", 0)) + 1
            if reason.strip():
                state["writer_reason"] = reason.strip()
            if was_closed:
                state.pop("closed_at", None)
                state["phase"] = "implement"
            return {"already_writer": False, "writer": self.role, "round": state["round"], "took_over": False}

        result = self.store.mutate(apply)
        self.store.append_transcript("claim", self.role, writer=self.role,
                                     reason=reason.strip() or None, result=result)
        # This note is read by the agent the instant it becomes the writer, which makes it
        # the worst possible place to state a rule that is false in an audit round. pair-guard
        # cannot correct it: it rewrites the standing brief, not a tool result.
        if self.store.read().get("mode", "fix") == "audit":
            note = ("You are the writer, and this is an audit round (mode=audit), so you do NOT "
                    "edit project files, do not branch and do not commit. Run the audit with your "
                    "peer: keep findings in notes, refuse to record one the peer has not checked, "
                    "put confirmed ones in docs/backlog/, and advance the phase.")
        else:
            note = "You are the writer. Edit project files directly. Your peer stays read-only."
        return {
            "writer": result["writer"],
            "already_held": result["already_writer"],
            "round": result.get("round"),
            "took_over": result.get("took_over", False),
            # Не выиграл гонку, а получил роль по решению человека: разница важна,
            # потому что спор о роли тут неуместен.
            "by_policy": result.get("by_policy", False),
            "note": note,
        }

    def tool_pair_release(self, why: str = "") -> dict[str, Any]:
        """Give up write authority voluntarily. Only the holder may do this.

        This is the honest way to hand over: the holder decides the other one is
        better placed. A peer cannot take authority away, and cannot revoke it, but
        the holder can give it — that is a decision, not a transfer by message.
        """
        def apply(state: dict[str, Any]) -> dict[str, Any]:
            current = state.get("writer")
            if current != self.role:
                raise ProtocolError(
                    f"{self.role} does not hold write authority; {current!r} does.",
                    "A peer cannot revoke authority. Ask the human if this needs to change.",
                )
            state["writer"] = None
            state["writer_reason"] = ""
            return {"released": True}

        self.store.mutate(apply)
        self.store.append_transcript("release", self.role, reason=why.strip() or None)
        return {
            "released": True,
            "note": (
                "Authority is now unassigned. Nobody holds write authority. If you meant your peer "
                "to take over, say so in pair_send and let them call pair_claim; the human can also "
                "assign it with `pairctl assign <role>`."
            ),
        }

    def _ring(self, target: str, kind: str, detail: str) -> dict[str, Any]:
        """Knock on the peer's tmux pane so a sleeping agent wakes itself.

        An idle process cannot be woken any other way: it has to be handed the keys.
        That is the whole reason this exists. The body is already in the mailbox or in
        a note, so what gets typed is a short trigger, which is safe to push into a
        composer where a whole paragraph would be mangled.
        """
        if os.environ.get("PAIR_DISABLE_RING") == "1":
            return {"rung": False, "why": "ring disabled by PAIR_DISABLE_RING"}
        state = self.store.read()
        pane = (state.get("roles", {}).get(target) or {}).get("pane")
        if not pane:
            return {"rung": False, "why": "нет панели, сосед вне tmux"}
        if pane == os.environ.get("TMUX_PANE"):
            return {"rung": False, "why": "не звоним в собственную панель"}
        tmux = shutil.which("tmux")
        if not tmux:
            return {"rung": False, "why": "tmux не найден в PATH"}
        probe = subprocess.run(
            [tmux, "display-message", "-p", "-t", pane, "#{pane_dead}"],
            capture_output=True, text=True, timeout=5,
        )
        if probe.returncode != 0 or probe.stdout.strip() == "1":
            return {"rung": False, "why": "панель соседа мертва"}
        from composer import capture, composer_line, is_composer_empty
        deadline = time.monotonic() + 1.5
        while True:
            line = composer_line(capture(tmux, pane))
            # None — композер не виден, и это не то же самое, что «свободен».
            # `is_composer_empty(None)` падает на NoneType.rstrip, то есть любой
            # звонок в панель, где TUI перерисовался, ронял звонящего. Отсутствие
            # строки ввода — это «не трогать», ровно как в deliver().
            if line is not None and is_composer_empty(line):
                break
            if time.monotonic() >= deadline:
                reason = "композер соседа не виден, не перебиваем" if line is None \
                    else "композер соседа занят, не перебиваем"
                return {"rung": False, "why": reason}
            time.sleep(0.3)
        prompt = f"pair: {kind} от {self.role} — {detail}. Прочитай pair_read."
        if len(prompt) > 150:
            prompt = prompt[:147] + "..."
        subprocess.run([tmux, "send-keys", "-t", pane, "-l", prompt],
                       capture_output=True, text=True, timeout=5)
        subprocess.run(["tmux", "send-keys", "-t", pane, "Enter"],
                       capture_output=True, text=True, timeout=5)
        self.store.append_transcript("doorbell", self.role, to=target, bell_kind=kind, pane=pane)
        return {"rung": True, "pane": pane, "prompt": prompt}

    def tool_pair_unblock(self, peer: str = "") -> dict[str, Any]:
        """Check whether a peer's tmux pane is stuck on a permission prompt, and
        confirm it only if the prompt is on the allowlist.

        Use when the peer agent has stalled or silent_seconds is growing, to prevent
        sessions from blocking on CLI prompts.

        The allowlist is why this is not a free "y". Anything that writes, runs or
        deletes is refused by default and reported, because an agent left alone with
        a destructive prompt is better than one that says yes on your behalf.
        """
        from composer import _tmux, resolve_permission_prompt

        target = peer.strip()
        state = self.store.read()
        roles = state.get("roles", {})
        if not target:
            candidates = [r for r in roles if r != self.role]
            if len(candidates) == 1:
                target = candidates[0]
            elif not candidates:
                return {"unblocked": False, "why": "нет зарегистрированных соседей", "prompt": ""}
            else:
                raise ProtocolError("`peer` is required when multiple peers are registered.")
        if target == self.role:
            raise ProtocolError("cannot unblock own role.")
        pane = (roles.get(target) or {}).get("pane")
        if not pane:
            return {"unblocked": False, "why": f"у {target} нет панели tmux", "prompt": ""}
        try:
            tmux = _tmux()
        except Exception as exc:
            return {"unblocked": False, "why": str(exc), "prompt": ""}
        allow, on_default = load_allowlist(self.store.root)
        # `allow or None` здесь раньше превращал дефолтный список в DEFAULT, а
        # пустой — в «подтверждать ничего». Теперь список всегда осмысленный: пустой
        # значит явный запрет от человека, а не отсутствие файла.
        try:
            result = resolve_permission_prompt(tmux, pane, allow=allow)
            if on_default and result.get("approved"):
                self.store.append_transcript(
                    "unblock_default_allowlist", self.role, target=target, pane=pane,
                    prompt=result.get("prompt"), matched=result.get("matched"),
                    note="permissions.json отсутствует, подтверждено по списку по умолчанию")
        except Exception as exc:  # noqa: BLE001 - чужая панель не должна ронять сервер
            return {"unblocked": False, "why": f"панель не прочиталась: {exc}", "prompt": ""}
        matched = result.get("prompt") or ""
        if result.get("approved"):
            self.store.append_transcript("unblock", self.role, target=target, pane=pane,
                                         prompt=matched, matched=result.get("matched"))
        elif result.get("verdict") == "refuse":
            self.store.append_transcript("unblock_refused", self.role, target=target, pane=pane,
                                         prompt=matched, why=result.get("why"))
        return {
            "unblocked": bool(result.get("approved")),
            "peer": target,
            "pane": pane,
            "prompt": matched,
            "verdict": result.get("verdict"),
            "why": result.get("why"),
            "status": (
                f"Подтверждение [y] отправлено: {matched}"
                if result.get("approved")
                else f"Запрос разрешения НЕ подтверждён ({result.get('why')}): {matched}"
                if result.get("verdict") == "refuse"
                else "Запрос разрешения не обнаружен в панели"
            ),
        }

    def tool_pair_deliver(self, to: str, text: str, wait_for_empty: float = 0.0, submit: bool = True) -> dict[str, Any]:
        """Type a message straight into the peer's composer, as if the user typed it.

        The mailbox is passive: a message sits there until the peer thinks to look.
        This is the active path — the text appears in its prompt, so it wakes on its own
        and the exchange reads like two people talking.

        Refuses when the composer is busy rather than overtyping what is being written.
        Chunks the text, verifies the tail landed, and backspaces it out if not.
        """
        from composer import ComposerBusy, deliver

        if not isinstance(to, str) or not to.strip():
            raise ProtocolError("`to` is required.")
        to = to.strip()
        if to == self.role:
            raise ProtocolError("cannot deliver a message to your own role.")
        if not isinstance(text, str) or not text.strip():
            raise ProtocolError("`text` is required.")

        state = self.store.read()
        pane = (state.get("roles", {}).get(to) or {}).get("pane")
        if not pane:
            return {"delivered": False, "why": f"у {to} нет панели tmux, он вне tmux"}
        try:
            result = deliver(pane, text, wait_for_empty=wait_for_empty, submit=submit)
        except ComposerBusy as exc:
            self.store.append_transcript("deliver_busy", self.role, to=to, reason=str(exc))
            send_res = self.tool_pair_send(to=to, text=text, subject="[mailbox fallback: composer busy]")
            return {
                "delivered": False,
                "why": str(exc),
                "fallback_sent": True,
                "seq": send_res.get("seq"),
            }
        self.store.append_transcript("deliver", self.role, to=to, **result)
        result["delivered_to_pane"] = pane
        if result.get("unverified"):
            # Панель не подтвердила вставку, и композер при этом мог остаться с
            # текстом. Убирать его откатом нельзя — это чужой ввод, — поэтому
            # сообщение уходит в ящик тем же путём, что и при занятом композере.
            # Дубликат дешевле потери: сосед прочитает лишнее, а не пропустит.
            send_res = self.tool_pair_send(
                to=to, text=text, subject="[mailbox fallback: вставка не подтвердилась]")
            result["fallback_sent"] = True
            result["fallback_seq"] = send_res.get("seq")
            result["why"] = result["why"] + "; сообщение продублировано в ящик"
        return result

    def _obligations_where(self, side: str, role: str | None) -> list[dict[str, Any]]:
        """Open obligations, optionally filtered to one role.

        Kept small on purpose: a long list of stale obligations is noise, and the
        supervisor is what actually chases them.
        """
        out: list[dict[str, Any]] = []
        if not self.store.obligations_root.is_dir():
            return out
        for path in sorted(self.store.obligations_root.glob("*.json")):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if role is not None and record.get(side) != role:
                continue
            out.append({"seq": record.get("seq"), "from": record.get("from"),
                        "to": record.get("to"), "opened_at": record.get("opened_at"),
                        "subject": record.get("subject", "")})
        return out[:20]

    def _open_obligation(self, frm: str, to: str, seq: int, subject: str = "") -> dict[str, Any]:
        """Record that `frm` is waiting for an answer from `to`.

        The protocol's blind spot was exactly this: a message asking for a decision was
        indistinguishable from small talk, so a request nobody answered looked like a quiet
        evening. An obligation is a plain file, so the supervisor can read it without
        understanding any of the conversation.
        """
        self.store.obligations_root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = self.store.obligations_root / f"{seq:06d}-{stamp}-{frm}-{to}.json"
        record = {"seq": seq, "from": frm, "to": to, "opened_at": utcnow(), "subject": subject}
        atomic_write_json(path, record)
        self.store.append_transcript("obligation_opened", frm, to=to, seq=seq, subject=subject)
        return record

    def _answer_obligations(self, answerer: str, author: str) -> list[int]:
        """Called when `answerer` writes to `author`: whatever they owed each other closes.

        Any later message counts as the answer, including a fresh request, because two agents
        talking have by definition stopped waiting on each other. Judging the content would be
        guesswork, and a wrong guess here means a real request gets nagged about at 3am.
        """
        closed: list[int] = []
        for path in sorted(self.store.obligations_root.glob("*.json")) if self.store.obligations_root.is_dir() else []:
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if record.get("from") == answerer and record.get("to") == author:
                path.unlink(missing_ok=True)
                closed.append(int(record.get("seq", 0)))
        if closed:
            self.store.append_transcript("obligation_answered", answerer, author=author, seqs=closed)
        return closed

    def tool_pair_send(self, to: str, text: str, subject: str = "",
                        expects_reply: bool = False) -> dict[str, Any]:
        """Send one message to the peer. The label identifying you is added here,        not by you, so a peer cannot forge authorship.

        Set `expects_reply` when the message asks for a decision, a review or an action. That
        writes an obligation the supervisor watches, so a request nobody answered cannot end the
        night in silence.
        """
        if not isinstance(to, str) or not to.strip():
            raise ProtocolError("`to` is required.")
        to = to.strip()
        if not isinstance(text, str) or not text.strip():
            raise ProtocolError("`text` is required.")
        if to == self.role:
            raise ProtocolError("cannot send a message to your own role.")

        def apply(state: dict[str, Any]) -> dict[str, Any]:
            self._register(state)
            roles = state.setdefault("roles", {})
            roles[self.role]["sends"] = int(roles[self.role].get("sends", 0)) + 1
            if to not in roles:
                # Not fatal: the peer may not have started yet. Recorded for auditing.
                pass
            inbox = self.store.mailbox_root / to
            inbox.mkdir(parents=True, exist_ok=True)
            existing_seqs = [0]
            if self.store.mailbox_root.is_dir():
                for f in self.store.mailbox_root.glob("*/*.json"):
                    try:
                        existing_seqs.append(int(f.name.split("-")[0]))
                    except Exception:
                        pass
            current_max_seq = max(existing_seqs)
            seq = max(state.get("seq", 0), current_max_seq) + 1
            state["seq"] = seq
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            token = uuid.uuid4().hex[:6]
            filename = f"{seq:06d}-{stamp}-{self.role}-{token}.json"
            body = {
                "seq": seq,
                "from": self.role,
                "to": to,
                "ts": utcnow(),
                "subject": subject or "",
                "label": f"Chat from {self.role}: ",
                "text": text.strip(),
            }
            atomic_write_json(inbox / filename, body)
            return {"seq": seq, "known_target": to in roles}

        closed = self._answer_obligations(to, self.role)
        result = self.store.mutate(apply)
        self.store.append_transcript("send", self.role, to=to, seq=result["seq"], subject=subject)
        obligation = None
        if expects_reply:
            obligation = self._open_obligation(self.role, to, seq=result["seq"],
                                               subject=subject or text.strip()[:60])
        ring = self._ring(to, "сообщение", subject or text.strip()[:50])
        return {
            "delivered": True,
            "woke_peer": ring["rung"],
            "wake": ring.get("prompt") or ring.get("why"),
            "seq": result["seq"],
            "awaits_answer": expects_reply,
            "obligation": obligation,
            "target_is_registered": result["known_target"],
            "label_applied": f"Chat from {self.role}: ",
            "note": (
                "Delivered. Do not wait for a reply and do not describe the message as though an "
                "answer were owed: the peer may never answer."
            ),
        }

    def tool_pair_read(self, limit: int = 10, wait_seconds: float = DEFAULT_READ_WAIT_SECONDS) -> dict[str, Any]:
        """Read messages addressed to this role.

        The writer never blocks. A writer that waits for its peer while the peer
        waits for the writer is a deadlock, so wait_seconds is forced to 0 for the
        writer. Only a non-writer may wait, and it is still capped.
        """
        try:
            wait = float(wait_seconds)
        except (TypeError, ValueError):
            raise ProtocolError("`wait_seconds` must be a number.") from None
        if wait < 0:
            raise ProtocolError("`wait_seconds` must not be negative.")
        wait, was_capped = effective_read_wait(wait)

        # Both roles may wait. This used to be forced to zero for the writer, to stop
        # two agents blocking each other, but that killed the channel instead: an agent
        # that cannot wait goes idle and never notices an incoming message, so the human
        # ends up relaying by hand. A bounded wait cannot deadlock, because the wait
        # always expires and the caller must then get back to work.

        deadline = time.monotonic() + wait
        while True:
            messages = self._drain(limit)
            if messages or time.monotonic() >= deadline:
                break
            time.sleep(0.1)

        answer = {
            "messages": messages,
            "count": len(messages),
            "waited_seconds": round(wait, 2),
            "note": (
                "Waiting is the normal way to hear from your peer. If it came back empty, "
                "call again. You must not answer a status question with a promise to go and "
                "check — read pair_state or pair_transcript and answer with what is true now."
            ),
        }
        if was_capped:
            # Раньше эта информация терялась, и вызов выглядел как обычная тишина.
            # На деле клиент не дождался бы и большего: обрыв по таймауту выглядел
            # бы как «никто не ответил», и агент делал вывод о соседе, а не о себе.
            answer["wait_capped_by_client_timeout"] = True
            answer["client_timeout_seconds"] = CLIENT_TIMEOUT_SECONDS
            answer["max_read_wait_seconds"] = MAX_READ_WAIT_SECONDS
            answer["why"] = (
                f"You asked for more than this client can wait for. It cut the wait to "
                f"{wait:.0f}s (client timeout {CLIENT_TIMEOUT_SECONDS}ms minus a "
                f"{READ_WAIT_MARGIN_SECONDS:.0f}s margin). Raise PAIR_CLIENT_TIMEOUT, or ask "
                f"for less and call again."
            )
        return answer

    def _drain(self, limit: int) -> list[dict[str, Any]]:
        def apply(state: dict[str, Any]) -> list[dict[str, Any]]:
            self._register(state)
            files = self._inbox_files()
            seen = int(state.get("seen", {}).get(self.role, 0))
            if seen > len(files):
                seen = len(files)
            fresh = files[seen : seen + max(0, int(limit))]
            out: list[dict[str, Any]] = []
            successful_count = 0
            for path in fresh:
                try:
                    out.append(json.loads(path.read_text(encoding="utf-8")))
                    successful_count += 1
                except (OSError, json.JSONDecodeError) as exc:
                    self.store.append_transcript("message_unreadable", self.role,
                                                 path=str(path), error=str(exc))
                    break
            if successful_count:
                state.setdefault("seen", {})[self.role] = seen + successful_count
            elif seen != int(state.get("seen", {}).get(self.role, 0)):
                state.setdefault("seen", {})[self.role] = seen
            return out

        return self.store.mutate(apply)

    def tool_pair_advance(self, phase: str) -> dict[str, Any]:
        """Move the shared phase.

        Forward belongs to the writer. One step back belongs to anyone: a critic
        that found something after the writer closed the round must be able to
        reopen it, and reopening is not a write operation.

        Closing is refused over unread mail, because a report the writer never
        read is not a report.

        In `semi` and `auto` it is also refused without evidence: a round that no
        machine ever checked is a round that finished by opinion. Confident models
        report 100% certainty on most of their own estimates, so "everyone seemed
        happy" is not a signal the gate can accept. §12 stops the pair in every
        autonomy level and is not something this gate can wave through.
        """
        if phase not in PHASES:
            raise ProtocolError(f"unknown phase {phase!r}; expected one of {', '.join(PHASES)}.")

        current = self.store.read()
        from_phase = current.get("phase", "scoping")
        is_writer = current.get("writer") == self.role
        backward = PHASE_BACKWARD.get(from_phase)

        if not is_writer and phase != backward:
            hint = f'You may reopen the round: pair_advance("{backward}")' if backward else ""
            raise ProtocolError(
                f"only the writer moves the phase forward; {current.get('writer')!r} holds authority.",
                hint,
            )

        forward = PHASE_TRANSITIONS.get(from_phase, ())
        if phase not in forward and phase != backward:
            allowed = list(forward)
            if backward and backward not in allowed:
                allowed.append(backward)
            raise ProtocolError(
                f"cannot move from {from_phase!r} to {phase!r}; allowed: {', '.join(allowed) or 'none'}.",
                "Phases move scoping → plan → implement → verify → done. Any role may take one step back.",
            )

        reopening = from_phase == "done" and phase != "done"

        def apply(state: dict[str, Any]) -> None:
            if phase == "done":
                unread = self._unread_count(state)
                if unread > 0:
                    raise ProtocolError(
                        f"{unread} unread message(s) in your inbox; a round cannot be closed over unread mail.",
                        "Run pair_read first. If the peer still needs something, it has not been said yet.",
                    )
                blockers = self._done_blockers(state)
                if blockers:
                    raise ProtocolError(
                        "this round cannot be closed yet: " + " ".join(blockers),
                        "Everything here is checkable. Record the proof, or say in a note which "
                        "part of the task is knowingly left open and why.",
                    )
            state["phase"] = phase
            if reopening:
                state.pop("closed_at", None)
                state["reopened_by"] = self.role
                state["round"] = int(state.get("round", 0)) + 1
            elif phase == "done":
                state["closed_at"] = utcnow()

        self.store.mutate(apply)
        self.store.append_transcript(
            "advance", self.role, phase=phase, previous=from_phase, reopened=reopening
        )
        return {
            "phase": phase,
            "previous": from_phase,
            "reopened": reopening,
            "round": self.store.read().get("round", 0),
        }

    def tool_pair_task(self, action: str, id: int | None = None, text: str = "",
                        reason: str = "") -> dict[str, Any]:
        """Общий список задач: одна правда на двоих, а не копия у каждого.

        Список не заменяет заметки, а закрывает конкретную дыру: задача, добавленная
        в середине раунда, молча исчезала, потому что state хранил одну строку task
        и новая перезаписывала старую. Проверено: раунд закрывался как «задача Б» с
        доказательствами, собранными по задаче А.

        Права несимметричные, и это решение, а не недоработка: критик задачу
        заводит — иначе он не сможет сказать «я нашёл ещё вот это», не печатая
        в чужую заметку, — но закрывает её писатель, потому что реализует её он.
        """
        action = str(action or "").strip()
        if action not in ("add", "done", "drop", "list"):
            raise ProtocolError(f"unknown task action {action!r}. Use add, done, drop or list.")
        state = self.store.read()

        if action == "list":
            items = tasks_of(state)
            return {
                "tasks": items,
                "open": [item.get("id") for item in items if item.get("status") == OPEN_TASK],
                "next_id": next_task_id(state),
            }

        if action == "add":
            body = str(text or "").strip()
            if not body:
                raise ProtocolError("`text` is required: what has to be done.")
            if len(body) > 2000:
                raise ProtocolError("A task longer than 2000 characters is not a task.")

            def add(st):
                item = {
                    "id": next_task_id(st), "text": body, "status": OPEN_TASK,
                    "added_at": utcnow(), "added_by": self.role,
                    "round": int(st.get("round", 0)), "reason": "",
                    "closed_at": None, "closed_by": None,
                }
                st.setdefault("tasks", []).append(item)
                return item

            item = self.store.mutate(add)
            self.store.append_transcript("task_added", self.role, task_id=item["id"],
                                         text=body[:200])
            return {"task": item,
                    "why_it_matters": "An open task keeps the round from closing until it is "
                                      "done or dropped with a reason."}

        # done / drop — только у писателя.
        if state.get("writer") != self.role:
            raise ProtocolError(
                f"only the writer closes tasks; {self.role!r} may add and list them.",
                "You found it — add it with pair_task(action=\"add\", text=...) and the writer "
                "will implement it. Closing someone's task is the writer's call.",
            )
        target = int(id or 0)

        def close(st):
            items = st.setdefault("tasks", [])
            match = next((item for item in items if int(item.get("id", 0)) == target), None)
            if not match:
                known = ", ".join(str(item.get("id")) for item in items) or "none yet"
                raise ProtocolError(f"no task #{target}. Known ids: {known}.")
            if match.get("status") != OPEN_TASK:
                raise ProtocolError(
                    f"task #{target} is already {match.get('status')!r}.",
                    "Nothing to change. If it came back up, add a new task that says so.",
                )
            if action == "drop" and not str(reason or "").strip():
                raise ProtocolError(
                    f"task #{target} cannot be dropped without a reason.",
                    "Say in the reason why it is not needed, so the record shows the decision "
                    "instead of a silent gap. If it is done, use action=\"done\".",
                )
            match["status"] = "done" if action == "done" else "dropped"
            match["reason"] = str(reason or "").strip()
            match["closed_at"] = utcnow()
            match["closed_by"] = self.role
            return match

        item = self.store.mutate(close)
        self.store.append_transcript("task_closed", self.role, task_id=item["id"],
                                     status=item["status"], reason=item["reason"][:200])
        remaining = [i["id"] for i in open_tasks(self.store.read())]
        return {"task": item, "still_open": remaining,
                "how_to_use": "Closing is honest bookkeeping. If the work is not actually "
                              "finished, leave it open or drop it with a reason — the round "
                              "cannot close over an open task, and that is the point."}

    def tool_pair_evidence(self, kind: str, value: str, ok: bool = True,
                           note: str = "", task: int | None = None) -> dict[str, Any]:
        """Record a fact a machine produced: tests, lint, a typecheck, a commit, a PR.

        This is what makes a round a result instead of an opinion. `value` is the
        machine's own output — "117 passed in 6.2s", "exit 0", a commit sha, a PR
        URL. `ok=false` is not a confession of failure, it is the record that stops
        the round from closing until somebody deals with it.

        Evidence belongs to the round it was gathered in. Closing round 7 on what
        proved round 3 is the thing this exists to prevent.
        """
        kind = (kind or "").strip()
        if kind not in EVIDENCE_KINDS:
            raise ProtocolError(
                f"`kind` must be one of {', '.join(EVIDENCE_KINDS)}.",
                "Use 'manual' for something a human checked by hand, and say in `note` what "
                "they looked at. An invented kind cannot be checked by anyone.",
            )
        if not str(value or "").strip():
            raise ProtocolError("`value` is required: the machine's own output, not a verdict.")

        def apply(state: dict[str, Any]) -> dict[str, Any]:
            # К какой задаче относится факт. По умолчанию — единственная открытая:
            # обычно в работе она одна, и спрашивать каждый раз значило бы
            # заставлять агента повторять то, что уже следует из списка.
            still_open = [item["id"] for item in open_tasks(state)]
            owner = int(task) if task is not None else (still_open[0] if len(still_open) == 1 else None)
            record = {
                "round": int(state.get("round", 0)),
                "role": self.role,
                "kind": kind,
                "value": str(value).strip(),
                "ok": bool(ok),
                "note": str(note or "").strip(),
                "task": owner,
                # Снимок открытых задач в момент записи. Если их было несколько,
                # факт не привязан ни к одной — врать не хочется, но и терять
                # его нельзя: по этому списку журнал поймёт, к чему относилось.
                "tasks_open": still_open,
                "at": utcnow(),
            }
            entries = state.setdefault("evidence", [])
            entries.append(record)
            # Записи текущего раунда не вытесняются никогда. Гейт закрытия читает
            # их из state, а не из транскрипта, поэтому вытесненная запись о падении
            # просто перестаёт учитываться: раунд с красными тестами закрывался
            # потому, что журнал переполнился. Полоса для прошлых раундов остаётся,
            # иначе state растёт бесконечно.
            current = int(state.get("round", 0))
            older = [item for item in entries if int(item.get("round", 0)) != current]
            mine = [item for item in entries if int(item.get("round", 0)) == current]
            del older[: max(0, len(older) - EVIDENCE_KEEP)]
            state["evidence"] = older + mine
            return record

        record = self.store.mutate(apply)
        self.store.append_transcript("evidence", self.role, evidence_kind=kind,
                                     value=record["value"][:200], ok=record["ok"],
                                     round=record["round"])
        state = self.store.read()
        failed = [item for item in self.round_evidence(state) if not item.get("ok")]
        passed = [item for item in self.round_evidence(state) if item.get("ok")]
        return {
            "recorded": record,
            "round": record["round"],
            "passed_this_round": len(passed),
            "failing_this_round": len(failed),
            "closing_blocked_by": ([f"{item.get('kind')}: {item.get('value')}" for item in failed]
                                   or []),
        }

    # ------------------------------------------------------------ patch flow

    def tool_pair_patch_reserve(self) -> dict[str, Any]:
        """Reserve an empty patch file. Refused for the writer, which edits directly."""
        self._require_fix_mode("propose a patch")
        self._require_not_writer("propose patches")
        handle, name = tempfile.mkstemp(
            dir=str(self.store.patches_root), prefix=f"pair-{self.role}-", suffix=".patch"
        )
        os.close(handle)
        path = Path(name)
        os.chmod(path, 0o600)

        def apply(state: dict[str, Any]) -> None:
            state.setdefault("patches", {})[str(path)] = {
                "owner": self.role,
                "created_at": utcnow(),
                "sealed": False,
                "sha256": None,
                "applied": False,
            }

        self.store.mutate(apply)
        self.store.append_transcript("patch_reserve", self.role, path=str(path))
        return {
            "path": str(path),
            "mode": "0600",
            "how_to_use": [
                "Write the unified diff into this exact path with your normal file tools.",
                "Then call pair_patch_seal with this path to compute and record its SHA-256.",
                "Send the peer the path and the hash. Do not apply it yourself.",
            ],
        }

    def _owned_patch(self, state: dict[str, Any], path: str) -> dict[str, Any]:
        """Patch record, for actions only the proposing role may perform."""
        record = state.get("patches", {}).get(path)
        if not record:
            raise ProtocolError(f"{path} was not reserved through pair_patch_reserve.")
        if record.get("owner") != self.role:
            raise ProtocolError(f"{path} belongs to {record.get('owner')!r}, not {self.role!r}.")
        return record

    def _any_patch(self, state: dict[str, Any], path: str) -> dict[str, Any]:
        """Patch record for the writer, who verifies and applies patches proposed
        by the peer and therefore never owns them."""
        record = state.get("patches", {}).get(path)
        if not record:
            raise ProtocolError(f"{path} was not reserved through pair_patch_reserve.")
        return record

    def _releasable_patch(self, state: dict[str, Any], path: str) -> tuple[dict[str, Any], bool]:
        """Запись патча, который эта роль имеет право снять, и сняла ли его писатель.

        Своё — всегда. Чужое — только у писателя и только пока патч не решён.

        Раньше чужой патч не мог снять никто: автор может, но если он ушёл —
        некому. `pair_patch_applied` не помогает, он требует `verified_match`,
        а файла может уже не быть. На этом раунд закрывался невозможно, и я
        наступил на это вживую: патч соседа висел, а сервер отказал мне в
        `pair_patch_release`. Запись в транскрипте при этом обязательна —
        «снял писатель» и «снял автор» это разные вещи, и отчёт обязан их различать.
        """
        record = state.get("patches", {}).get(path)
        if not record:
            raise ProtocolError(f"{path} was not reserved through pair_patch_reserve.")
        if record.get("owner") == self.role:
            return record, False
        if state.get("writer") != self.role:
            raise ProtocolError(
                f"{path} belongs to {record.get('owner')!r}, not {self.role!r}.",
                "Only the writer may drop a patch they did not propose.",
            )
        if record.get("applied"):
            raise ProtocolError(
                f"{path} is already recorded as applied by {record.get('applied_by')!r}.",
                "An applied patch is not unresolved. If the apply was wrong, say so in a note "
                "and re-apply by hand; dropping the record here would hide both.",
            )
        return record, True

    def tool_pair_patch_seal(self, path: str) -> dict[str, Any]:
        """Compute and record the hash of a patch this role reserved."""
        if any(char in path for char in GLOB_CHARS):
            raise ProtocolError(f"{path} contains a glob character; exact paths only.")

        def apply(state: dict[str, Any]) -> dict[str, Any]:
            record = self._owned_patch(state, path)
            target = Path(path)
            if not target.is_file():
                raise ProtocolError(f"{path} does not exist.")
            digest = sha256_of(target)
            record["sealed"] = True
            record["sha256"] = digest
            record["sealed_at"] = utcnow()
            record["size"] = target.stat().st_size
            return record

        record = self.store.mutate(apply)
        self.store.append_transcript("patch_seal", self.role, path=path, sha256=record["sha256"])
        return {
            "path": path,
            "sha256": record["sha256"],
            "size": record.get("size"),
            "next": (
                f"Send the peer exactly: {path} sha256 {record['sha256']}. "
                "They must re-verify it immediately before applying."
            ),
        }

    def tool_pair_patch_verify(self, path: str) -> dict[str, Any]:
        """Re-check a sealed patch against its recorded hash. The writer calls this
        immediately before applying; anything other than a match means stop."""
        if any(char in path for char in GLOB_CHARS):
            raise ProtocolError(f"{path} contains a glob character; exact paths only.")
        current = self.store.read()
        if current.get("writer") != self.role:
            raise ProtocolError(
                "only the writer verifies and applies patches.",
                "You proposed it; the writer owns applying it.",
            )

        def apply(state: dict[str, Any]) -> dict[str, Any]:
            record = self._any_patch(state, path)
            if not record.get("sealed"):
                raise ProtocolError(f"{path} was never sealed; there is no hash to check against.")
            target = Path(path)
            if not target.is_file():
                raise ProtocolError(f"{path} has disappeared since it was sealed.")
            actual = sha256_of(target)
            record["verified_at"] = utcnow()
            record["verified_match"] = actual == record.get("sha256")
            record["sha256_at_verify"] = actual
            return record

        record = self.store.mutate(apply)
        match = bool(record.get("verified_match"))
        self.store.append_transcript("patch_verify", self.role, path=path, match=match)
        if not match:
            log(f"HASH MISMATCH on {path}: recorded {record.get('sha256')}, found {record.get('sha256_at_verify')}")
        return {
            "path": path,
            "match": match,
            "recorded_sha256": record.get("sha256"),
            "actual_sha256": record.get("sha256_at_verify"),
            "verdict": (
                "Hash matches. Copy the patch once, apply from the copy, then re-verify before applying."
                if match
                else "STOP. The patch changed after it was sealed. Do not apply it; ask the peer to re-seal."
            ),
        }

    def tool_pair_patch_applied(self, path: str, ok: bool = True, note: str = "") -> dict[str, Any]:
        """Record the outcome of applying a sealed patch. Writer only.

        The protocol had no verb for this: a patch was sealed and verified, and
        nothing ever said whether it reached the tree. A round could therefore
        close over a proposal that had been sitting untouched since yesterday, and
        nothing in the state, the transcript or the report would admit it. Closing
        a round now requires that every sealed patch is accounted for.

        `ok=false` means the apply failed — a rejected hunk, a conflict, a context
        that no longer matched. That counts as unresolved, so the round stays open
        until it is fixed or the patch is released with a reason.
        """
        if any(char in path for char in GLOB_CHARS):
            raise ProtocolError(f"{path} contains a glob character; exact paths only.")
        current = self.store.read()
        if current.get("writer") != self.role:
            raise ProtocolError(
                "only the writer applies patches.",
                "You proposed it; the writer owns applying it and recording the outcome.",
            )

        def apply(state: dict[str, Any]) -> dict[str, Any]:
            record = self._any_patch(state, path)
            if not record.get("sealed"):
                raise ProtocolError(f"{path} was never sealed, so there is nothing to have applied.")
            if not record.get("verified_match"):
                raise ProtocolError(
                    f"{path} was never verified against its sealed hash.",
                    "Call pair_patch_verify first. Applying a patch nobody re-hashed is how a "
                    "changed proposal gets into the tree under the wrong hash.",
                )
            record["applied"] = bool(ok)
            record["applied_at"] = utcnow()
            record["applied_by"] = self.role
            if note:
                record["applied_note"] = str(note)[:400]
            return record

        record = self.store.mutate(apply)
        self.store.append_transcript("patch_applied", self.role, path=path, ok=bool(ok),
                                     sha256=record.get("sha256"))
        remaining = self.unresolved_patches(self.store.read())
        return {
            "path": path,
            "applied": bool(ok),
            "applied_by": self.role,
            "sha256": record.get("sha256"),
            "still_unresolved": [item["path"] for item in remaining],
            "how_to_use": (
                "Recorded. If the apply failed, fix it and record the passing outcome, or "
                "pair_patch_release with the reason so the round is not held open by a patch "
                "both sides already abandoned."
            ) if not ok else "Recorded. This patch no longer blocks closing the round.",
        }

    def tool_pair_patch_release(self, path: str) -> dict[str, Any]:
        """Delete a patch file and forget its record, by exact path.

        Own patch: always. Someone else's: the writer may, while it is still
        unresolved, and the transcript says the writer did it. A file already gone
        from disk is not a problem — the record is what blocks the round, and this
        is the only thing that removes it.
        """
        if any(char in path for char in GLOB_CHARS):
            raise ProtocolError(
                f"{path} contains a glob character.",
                "Release patches by exact path. Never clean a directory with a glob: two agents "
                "share it and one of them may hold a file you cannot see.",
            )

        def apply(state: dict[str, Any]) -> bool:
            record, by_writer = self._releasable_patch(state, path)
            # The record leaves state (state describes the present, the transcript
            # describes history), but the drop itself is written to the audit trail:
            # "a sealed patch was thrown away" is a fact the report must be able to
            # show, and once the record is gone this is the only place it survives.
            self.store.append_transcript(
                "patch_dropped", self.role, path=path,
                owner=record.get("owner"), sha256=record.get("sha256"),
                sealed=bool(record.get("sealed")), applied=bool(record.get("applied")),
                released_by_writer=by_writer)
            del state["patches"][path]
            return bool(record)

        existed = self.store.mutate(apply)
        deleted = False
        target = Path(path)
        if target.exists():
            try:
                target.unlink()
                deleted = True
            except OSError as error:
                if error.errno != errno.ENOENT:
                    raise
        self.store.append_transcript("patch_release", self.role, path=path, existed=existed, deleted=deleted)
        return {"path": path, "deleted": deleted, "known_to_server": existed}

    def tool_pair_note(self, kind: str, text: str) -> dict[str, Any]:
        """Append a durable note: a plan, a decision, a finding, a verdict.

        Notes are the shared memory two agents leave behind. Anything one of them
        needs to survive a context reset belongs here, not in the conversation.
        """
        kind = (kind or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", kind):
            raise ProtocolError("`kind` must be a short slug: letters, digits, dot, dash, underscore.")
        if not (text or "").strip():
            raise ProtocolError("`text` is required.")
        self.store.notes_root.mkdir(parents=True, exist_ok=True)
        target = self.store.notes_root / f"{kind}.md"
        with self.store.lock():
            with target.open("a", encoding="utf-8") as stream:
                stream.write(f"\n## {utcnow()} — {self.role}\n\n{text.strip()}\n")
        self.store.append_transcript("note", self.role, note_kind=kind, chars=len(text))
        woke = {}
        for other in self.store.read().get("roles", {}):
            if other == self.role:
                continue
            r = self._ring(other, f"заметка/{kind}", text.strip()[:50])
            if r["rung"]:
                woke[other] = r["pane"]
        return {"kind": kind, "path": str(target), "woke_peers": woke}
    def invoke(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Run one tool by name, recording refusals on the audit trail.

        Every refusal lands in the transcript regardless of transport, so the
        trail answers "what did each agent try, and what was it told?" and not
        just "what succeeded". A peer probing for a way around the rules is
        exactly the event worth keeping.
        """
        handler = HANDLERS.get(name)
        if handler is None:
            raise ProtocolError(f"unknown tool {name!r}")
        try:
            return handler(self, arguments)
        except ProtocolError as exc:
            self.store.append_transcript("refusal", self.role, tool=name, reason=exc.message)
            raise

    def tool_pair_transcript(self, limit: int = 40) -> dict[str, Any]:
        """Read the append-only audit trail of everything both agents did."""
        entries = self.store.read_transcript(limit)
        return {"entries": entries, "count": len(entries)}


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --------------------------------------------------------------------- tools

def tool_definitions() -> list[dict[str, Any]]:
    role_note = "Your own role is fixed by configuration and cannot be overridden per call."
    return [
        {
            "name": "pair_deliver",
            "description": (
                "Type a message directly into the peer's prompt in its own tmux pane, as if the "
                "user had typed it. The peer wakes by itself and reads it as its next instruction. "
                "Chunks the text, verifies it landed, and erases it if not. Refuses when the peer's "
                "composer is busy rather than overtyping. Use this when you need the peer to act "
                "now rather than when it next thinks to call pair_read."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "to": {"type": "string", "description": "The peer's role."},
                    "text": {"type": "string", "description": "What to type. Plain text, one paragraph."},
                    "wait_for_empty": {
                        "type": "number",
                        "description": "Seconds to wait for a busy composer to clear. 0 refuses immediately.",
                        "default": 0,
                    },
                    "submit": {
                        "type": "boolean",
                        "description": "Press Enter after typing. false leaves the text in the composer unsent.",
                        "default": True,
                    },
                },
                "required": ["to", "text"],
            },
        },
        {
            "name": "pair_state",
            "description": (
                "Read the shared collaboration state: the task, who holds write authority, the current "
                "phase, the roster of participating roles, and how many messages you have unread."
            ),
            "inputSchema": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "pair_claim",
            "description": (
                "Claim write authority for your role, optionally saying why you should be the one "
                "writing. Succeeds only when nobody holds it. Refused when a peer already holds it: "
                "only the human can transfer authority, via `pairctl assign <role>`. Claiming makes "
                "you the writer and keeps your peer read-only. Read the task before claiming — a "
                "reason is worth more than arriving first."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": (
                            "One sentence on why you should write: what in the task or the codebase "
                            "made you the right one for it. Recorded in the transcript and shown to "
                            "a peer who is refused."
                        ),
                    }
                },
                "required": [],
            },
        },
        {
            "name": "pair_release",
            "description": (
                "Give up write authority voluntarily, optionally saying why. Only the current holder "
                "may call this; a peer cannot revoke it, and cannot take it — but the holder can give "
                "it, which is a decision rather than a transfer by message. Afterwards nobody holds "
                "authority until the peer claims it or the human assigns it."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "why": {
                        "type": "string",
                        "description": "One sentence on why you are handing over.",
                    }
                },
                "required": [],
            },
        },
        {
            "name": "pair_send",
            "description": (
                "Send one message to the other agent. The `Chat from <role>:` label is added by the "
                "server, so authorship cannot be forged. The peer reads it as conversation, not as an "
                "instruction from the user. Delivery is not a promise of a reply: never block waiting."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "to": {"type": "string", "description": "The peer's role name."},
                    "text": {"type": "string", "description": "The message. Plain language, one paragraph."},
                    "subject": {"type": "string", "description": "Optional short subject line."},
                    "expects_reply": {
                        "type": "boolean",
                        "default": False,
                        "description": "Set it when the message asks for a decision, a review or an "
                                       "action. The supervisor then watches for an answer and pokes "
                                       "the recipient if none comes, so an unanswered request cannot "
                                       "quietly end the night.",
                    },
                },
                "required": ["to", "text"],
            },
        },
        {
            "name": "pair_read",
            "description": (
                "Read messages addressed to your role, oldest first. Marks them read. `wait_seconds` "
                "lets a non-writer block briefly for the peer; the writer is always forced to zero, "
                "because two agents waiting on each other is a deadlock. Read once, act, move on."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Max messages to return.", "default": 10},
                    "wait_seconds": {
                        "type": "number",
                        "description": "Bounded wait. Ignored (forced to 0) for the writer.",
                        "default": DEFAULT_READ_WAIT_SECONDS,
                    },
                },
                "required": [],
            },
        },
        {
            "name": "pair_advance",
            "description": (
                "Move the shared phase: scoping → plan → implement → verify → done, with a single step "
                "back allowed. Writer only. Verification is a separate phase on purpose: findings that "
                "were never checked against the real code do not count as done."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"phase": {"type": "string", "enum": list(PHASES)}},
                "required": ["phase"],
            },
        },
        {
            "name": "pair_patch_reserve",
            "description": (
                "Reserve an empty mode-0600 patch file for proposing a change. Non-writers only: the "
                "writer edits project files directly and never needs this. Write your unified diff into "
                "the returned exact path, then call pair_patch_seal."
            ),
            "inputSchema": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "pair_patch_seal",
            "description": (
                "Compute and record the SHA-256 of a patch file you reserved, so the writer can detect "
                "any change between proposal and application. Call this after finishing the diff."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Exact path from pair_patch_reserve."}},
                "required": ["path"],
            },
        },
        {
            "name": "pair_patch_verify",
            "description": (
                "Re-hash a sealed patch and compare against the recorded value. The writer calls this "
                "immediately before applying. A mismatch means STOP: the patch changed after it was "
                "sealed and must not be applied."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Exact path of the sealed patch."}},
                "required": ["path"],
            },
        },
        {
            "name": "pair_patch_release",
            "description": (
                "Delete one of your own patch files, by exact path. Glob patterns are rejected: both "
                "agents share the patch directory and either may be holding a file you cannot see."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Exact path. No globs."}},
                "required": ["path"],
            },
        },
        {
            "name": "pair_task",
            "description": (
                "The shared task list. Add what you found, close what is finished. The round "
                "cannot close while any task is open, so a task added mid-round cannot be "
                "forgotten: either it gets done, or it gets dropped with a reason on the "
                "record. The critic adds tasks it discovered; only the writer closes them, "
                "because implementing them is the writer's job (PROTOCOL.md 4)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["add", "done", "drop", "list"]},
                    "id": {"type": "integer",
                           "description": "Which task, for done/drop. Omit for add and list."},
                    "text": {"type": "string", "description": "What to do. Required for add."},
                    "reason": {"type": "string",
                               "description": "Required for drop, and it may not be empty."},
                },
                "required": ["action"],
            },
        },
        {
            "name": "pair_evidence",
            "description": (
                "Record a machine-checkable fact about this round: the test suite with its "
                "count and exit code, lint, typecheck, a commit sha, a PR URL. In semi and "
                "auto the round cannot be closed without it, so 'it seems to work' has "
                "nowhere to go. ok=false records a failure honestly instead of hiding it, and "
                "blocks closing until it is resolved or explained in a note."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": list(EVIDENCE_KINDS)},
                    "value": {"type": "string",
                              "description": "The machine's own output, not your verdict. e.g. '117 passed, exit 0'."},
                    "ok": {"type": "boolean", "default": True,
                           "description": "false records a failure. It stays visible and blocks closing."},
                    "note": {"type": "string", "description": "Optional: how it was produced."},
                    "task": {"type": "integer",
                             "description": "Which task this proves. Defaults to the single open one."},
                },
                "required": ["kind", "value"],
            },
        },
        {
            "name": "pair_patch_applied",
            "description": (
                "Record the outcome of applying a sealed patch. Writer only, and only after "
                "pair_patch_verify matched. Without this the protocol had no way to say a "
                "patch had been applied, so a round could close over a proposal nobody ever "
                "touched. ok=false means the apply failed and stays unresolved."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Exact path of the sealed patch."},
                    "ok": {"type": "boolean", "default": True,
                           "description": "false if the apply failed. The patch then still blocks closing."},
                    "note": {"type": "string", "description": "Optional: hunk, commit, what went wrong."},
                },
                "required": ["path"],
            },
        },
        {
            "name": "pair_note",
            "description": (
                "Append a durable note to the shared notes directory under a slug kind, for example "
                "plan, decision, finding, verdict, handoff. This is the memory that survives a context "
                "reset, so anything the peer must still know goes here rather than into the chat."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "description": "Slug, e.g. plan, decision, finding, verdict."},
                    "text": {"type": "string", "description": "Markdown body."},
                },
                "required": ["kind", "text"],
            },
        },
        {
            "name": "pair_transcript",
            "description": (
                "Read the append-only audit trail: every message, claim, phase change, patch seal and "
                "hash verification either agent performed. Use it to reconstruct what actually happened."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"limit": {"type": "integer", "description": "Tail size.", "default": 40}},
                "required": [],
            },
        },
        {
            "name": "pair_unblock",
            "description": (
                "Check if a peer agent's terminal pane is blocked waiting on an interactive "
                "permission prompt (e.g. [y/n] or Allow). The prompt is confirmed only if it "
                "matches the permission allowlist — read-only tools by default — and a refusal "
                "is reported rather than silently pressed yes. Anything that writes, runs or "
                "deletes waits for the human."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "peer": {
                        "type": "string",
                        "description": "Role name of the peer to check and unblock (optional if only one peer is registered).",
                        "default": "",
                    },
                },
                "required": [],
            },
        },
    ]


HANDLERS = {
    "pair_state": lambda session, args: session.tool_pair_state(),
    "pair_deliver": lambda session, args: session.tool_pair_deliver(**args),
    "pair_claim": lambda session, args: session.tool_pair_claim(**args),
    "pair_release": lambda session, args: session.tool_pair_release(**args),
    "pair_send": lambda session, args: session.tool_pair_send(**args),
    "pair_read": lambda session, args: session.tool_pair_read(**args),
    "pair_advance": lambda session, args: session.tool_pair_advance(**args),
    "pair_patch_reserve": lambda session, args: session.tool_pair_patch_reserve(),
    "pair_patch_seal": lambda session, args: session.tool_pair_patch_seal(**args),
    "pair_patch_verify": lambda session, args: session.tool_pair_patch_verify(**args),
    "pair_patch_release": lambda session, args: session.tool_pair_patch_release(**args),
    "pair_patch_applied": lambda session, args: session.tool_pair_patch_applied(**args),
    "pair_task": lambda session, args: session.tool_pair_task(**args),
    "pair_evidence": lambda session, args: session.tool_pair_evidence(**args),
    "pair_note": lambda session, args: session.tool_pair_note(**args),
    "pair_transcript": lambda session, args: session.tool_pair_transcript(**args),
    "pair_unblock": lambda session, args: session.tool_pair_unblock(**args),
}


# ----------------------------------------------------------------- MCP server


class MCPServer:
    def __init__(self, session: PairSession) -> None:
        self.session = session
        self.initialized = False

    def send(self, payload: dict[str, Any]) -> None:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    def reply(self, request_id: Any, result: dict[str, Any]) -> None:
        self.send({"jsonrpc": "2.0", "id": request_id, "result": result})

    def error(self, request_id: Any, code: int, message: str, data: Any = None) -> None:
        error: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        self.send({"jsonrpc": "2.0", "id": request_id, "error": error})

    def run(self) -> None:
        log(f"ready role={self.session.role} state={self.session.store.root}")
        # readline() rather than iteration: iterating sys.stdin uses a read-ahead
        # buffer that withholds complete lines until the buffer fills, which on a
        # pipe deadlocks against a client waiting for our reply.
        while True:
            raw = sys.stdin.readline()
            if not raw:
                break
            raw = raw.strip()
            if not raw:
                continue
            try:
                message = json.loads(raw)
            except json.JSONDecodeError as exc:
                self.error(None, -32700, f"parse error: {exc}")
                continue
            if isinstance(message, list):
                for item in message:
                    self.dispatch(item)
                continue
            self.dispatch(message)
        log("stdin closed, exiting")

    def dispatch(self, message: Any) -> None:
        if not isinstance(message, dict):
            self.error(None, -32600, "request must be a JSON object")
            return
        method = message.get("method")
        request_id = message.get("id")
        if not isinstance(method, str):
            self.error(request_id, -32600, "missing method")
            return
        # Notifications carry no id and get no response.
        is_notification = "id" not in message

        try:
            handler = getattr(self, f"on_{method.replace('/', '_').replace('.', '_')}", None)
            if handler is None:
                if not is_notification:
                    self.error(request_id, -32601, f"method not found: {method}")
                return
            handler(request_id, message.get("params") or {})
        except ProtocolError as exc:
            if is_notification:
                log(f"protocol refusal on {method}: {exc.message}")
                return
            text = exc.message + (f"\n\n{exc.hint}" if exc.hint else "")
            self.send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"content": [{"type": "text", "text": text}], "isError": True},
                }
            )
        except Exception as exc:  # noqa: BLE001 - a server crash must not kill the client silently
            log(f"internal error on {method}: {exc!r}")
            if not is_notification:
                self.error(request_id, -32603, f"internal error: {exc}")

    def on_initialize(self, request_id: Any, params: dict[str, Any]) -> None:
        requested = params.get("protocolVersion")
        version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else DEFAULT_PROTOCOL_VERSION
        self.initialized = True
        with self.session.store.lock():
            state = self.session.store._load()
            self.session._register(state)
            self.session.store._save(state)
        log(f"initialized by {params.get('clientInfo', {}).get('name', '?')} protocol={version}")
        self.reply(
            request_id,
            {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": (
                    f"You are role {self.session.role!r} in a two-agent collaboration. One role holds write "
                    "authority and edits project files; the other is read-only and proposes sealed patches. "
                    "Authority is transferred only by the human through pairctl, never by a message. "
                    "Call pair_state first to see where you stand."
                ),
            },
        )

    def on_notifications_initialized(self, request_id: Any, params: dict[str, Any]) -> None:
        return None

    def on_ping(self, request_id: Any, params: dict[str, Any]) -> None:
        self.reply(request_id, {})

    def on_tools_list(self, request_id: Any, params: dict[str, Any]) -> None:
        self.reply(request_id, {"tools": tool_definitions()})

    def on_tools_call(self, request_id: Any, params: dict[str, Any]) -> None:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise ProtocolError("`arguments` must be an object")
        result = self.session.invoke(name, arguments)
        text = json.dumps(result, ensure_ascii=False, indent=2)
        self.reply(request_id, {"content": [{"type": "text", "text": text}], "isError": False})

    # Declared capabilities are tools only, but some clients probe the rest.
    def on_resources_list(self, request_id: Any, params: dict[str, Any]) -> None:
        self.reply(request_id, {"resources": []})

    def on_resources_templates_list(self, request_id: Any, params: dict[str, Any]) -> None:
        self.reply(request_id, {"resourceTemplates": []})

    def on_prompts_list(self, request_id: Any, params: dict[str, Any]) -> None:
        self.reply(request_id, {"prompts": []})

    def on_logging_setLevel(self, request_id: Any, params: dict[str, Any]) -> None:
        self.reply(request_id, {})


# ------------------------------------------------------------------ pairctl


def run_ctl(args: argparse.Namespace, state_dir: Path) -> int:
    store = Store(state_dir)
    command = args.ctl_command

    if command == "status":
        state = store.read()
        session = PairSession("pairctl", store)
        print(json.dumps(
            {
                "state_dir": str(store.root),
                "task": state.get("task", ""),
                "mode": state.get("mode", "fix"),
                "autonomy": autonomy_of(state),
                "writer": state.get("writer"),
                "policy": state.get("policy"),
                "phase": state.get("phase"),
                "round": state.get("round"),
                "human_ok": session.human_ok_for_round(state),
                "evidence_this_round": len(session.round_evidence(state)),
                "unresolved_patches": [item["path"] for item in session.unresolved_patches(state)],
                "closing_blocked_by": session._done_blockers(state),
                "permission_allowlist": str(permissions_path(store.root)),
                "roles": {name: info.get("last_seen") for name, info in state.get("roles", {}).items()},
            },
            indent=2,
            ensure_ascii=False,
        ))
        return 0

    if command == "assign":
        role = args.role
        known = set(store.read().get("roles", {}))
        if known and role not in known:
            print(f"warning: {role!r} has never connected; registered roles: {', '.join(sorted(known))}")
        def apply(state):
            state["writer"] = role
            # решение человека — повод поставить метку «выбрано человеком», чтобы
            # в переписке было видно, что это не гонка
            state["writer_reason"] = "назначено человеком"
            if state.get("closed_at") or state.get("phase") == "done":
                state.pop("closed_at", None)
                state["phase"] = "implement"
                state["round"] = int(state.get("round", 0)) + 1
            elif state.get("phase") == "scoping" and not state.get("task"):
                state["round"] = int(state.get("round", 0)) + 1
        store.mutate(apply)
        store.append_transcript("assign", "human", writer=role,
                                reason="назначено человеком")
        print(f"write authority assigned to {role}")
        return 0

    if command == "roles":
        # Постоянная расстановка: кто всегда пишет, кто всегда ревьюит.
        # Гонка остаётся как запасной вариант, но по умолчанию роли известны
        # заранее, и раунд начинается с работы, а не с торговли правами.
        policy_path = global_roles_path()
        state = store.read()
        current = state.get("policy") if isinstance(state.get("policy"), dict) else {}
        wanted = dict(current)

        if args.clear:
            wanted = {}
        else:
            if args.writer:
                wanted["writer"] = args.writer
            if args.critic:
                wanted["critic"] = args.critic
            if args.swap:
                was_writer, was_critic = wanted.get("writer"), wanted.get("critic")
                if was_writer or was_critic:
                    wanted["writer"] = was_critic
                    wanted["critic"] = was_writer

        if not wanted:
            store.mutate(lambda st: st.pop("policy", None))
            store.append_transcript("roles", "human", policy=None)
            print("постоянная расстановка снята, роли снова определяются гонкой")
            return 0

        if wanted.get("writer") == wanted.get("critic"):
            print("error: писатель и критик не могут быть одной ролью", file=sys.stderr)
            return 2

        store.mutate(lambda st: st.__setitem__("policy", wanted))
        store.append_transcript("roles", "human", policy=wanted, source=str(policy_path))

        # Расстановка сразу применяется к текущему раунду: назначенный писатель
        # получает право, иначе право остаётся у прошлого держателя и работа
        # стоит до вмешательства человека.
        holder = state.get("writer")
        if wanted.get("writer") and holder != wanted["writer"]:
            def apply(st):
                st["writer"] = wanted["writer"]
                st["writer_reason"] = "назначено человеком постоянной расстановкой"
            store.mutate(apply)

        if args.global_:
            policy_path.parent.mkdir(parents=True, exist_ok=True)
            policy_path.write_text(json.dumps(wanted, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"сохранено для любых проектов: {policy_path}")
        print(f"расстановка: писатель {wanted.get('writer') or '—'}, критик {wanted.get('critic') or '—'}")
        return 0

    if command == "roster":
        # Что записан в ростере, кто из этого ещё существует.
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
        living = {role: info.get("pane") for role, info in roles_now.items()
                  if isinstance(info.get("pid"), int) and info["pid"] > 0
                  and is_pid_alive(info["pid"])}
        entries = []
        for role, info in roles_now.items():
            pid = info.get("pid")
            pane = info.get("pane")
            pid_dead = isinstance(pid, int) and pid > 0 and not is_pid_alive(pid)
            pane_gone = bool(pane) and not pane_exists(pane)
            holder = next((other for other, other_pane in living.items()
                           if other != role and other_pane == pane), None)
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

    if command == "release":
        def apply(state):
            state["writer"] = None
            state["writer_reason"] = ""
        store.mutate(apply)
        store.append_transcript("release", "human")
        print("write authority cleared; nobody holds it")
        return 0

    if command == "mode":
        mode = args.mode
        if mode not in MODES:
            print(f"error: unknown mode {mode!r}; expected one of {', '.join(MODES)}", file=sys.stderr)
            return 2
        previous = store.read().get("mode", "fix")
        store.mutate(lambda state: state.__setitem__("mode", mode))
        store.append_transcript("mode", "human", mode=mode, previous=previous)
        print(f"round mode is now {mode!r}")
        return 0

    if command == "autonomy":
        level = args.level
        if level not in AUTONOMIES:
            print(f"error: unknown autonomy {level!r}; expected one of {', '.join(AUTONOMIES)}",
                  file=sys.stderr)
            return 2
        previous = autonomy_of(store.read())
        store.mutate(lambda state: state.__setitem__("autonomy", level))
        store.append_transcript("autonomy", "human", autonomy=level, previous=previous)
        notes = {
            "observe": "сверхвизор только сообщает: упавшего не перезапускает, разрешения не подтверждает",
            "semi": "работа идёт сама, но закрыть раунд без доказательств и без 'pairctl ok' нельзя",
            "auto": "работа идёт сама до закрытия раунда, нужен только записанный факт",
        }
        print(f"autonomy is now {level!r}: {notes[level]}")
        return 0

    if command == "ok":
        # Одно слово человека на чекпоинте. Именно это делает semi «полуавтоматом»:
        # пара работает сама, но закрыть раунд без него не может.
        #
        # --revoke отзывает сказанное. Отзыв нужен не для удобства: человек
        # смотрит на правки, говорит «ок» и видит, что после этого изменилось ещё
        # что-то. Без отзыва единственный выход — сменить настройку или reset.
        state = store.read()
        round_number = int(state.get("round", 0))
        revoke = bool(getattr(args, "revoke", False))

        def apply(st):
            entries = st.setdefault("human_ok", [])
            same_round = [e for e in entries if int(e.get("round", -1)) == round_number]
            if same_round and bool(same_round[-1].get("revoke")) == revoke:
                return False
            entries.append({"round": round_number, "at": utcnow(), "phase": st.get("phase"),
                            "revoke": revoke})
            del entries[:-20]
            return True

        fresh = store.mutate(apply)
        store.append_transcript("human_ok_revoke" if revoke else "human_ok", "human",
                                round=round_number, phase=state.get("phase"), changed=fresh)
        if not fresh:
            print(f"round {round_number}: {'already revoked' if revoke else 'already marked'}; "
                  f"nothing changed")
        elif revoke:
            print(f"ok revoked for round {round_number}; it cannot close until you say ok again")
        else:
            print(f"ok: round {round_number} marked at phase {state.get('phase')!r}")
            if state.get("phase") not in OK_PHASES:
                print(f"  note: ok counts only at {', '.join(OK_PHASES)}; "
                      f"this one will not close the round from {state.get('phase')!r}")
        return 0

    if command == "why":
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

    if command == "task":
        # Список, а не одна строка. Новая задача дописывается: старая не исчезает,
        # и раунд не закроется, пока что-то не помечено сделанным или брошенным с
        # причиной.
        if args.done or args.drop:
            target = int(args.done or args.drop)
            want = "done" if args.done else "dropped"

            def close(st):
                match = next((i for i in st.get("tasks") or []
                              if int(i.get("id", 0)) == target), None)
                if not match:
                    raise ProtocolError(f"no task #{target}")
                if want == "dropped" and not str(args.reason or "").strip():
                    raise ProtocolError("--drop needs --reason")
                match["status"] = want
                match["reason"] = str(args.reason or "").strip()
                match["closed_at"] = utcnow()
                match["closed_by"] = "human"
                return match

            item = store.mutate(close)
            store.append_transcript("task_closed", "human", task_id=item["id"], status=want)
            print(f"task #{item['id']} is {want}")
        else:
            body = " ".join(str(args.text or "").split())
            if not body:
                print("task needs text", file=sys.stderr)
                return 2

            def apply(state):
                item = {"id": next_task_id(state), "text": body, "status": OPEN_TASK,
                        "added_at": utcnow(), "added_by": "human",
                        "round": int(state.get("round", 0)), "reason": "",
                        "closed_at": None, "closed_by": None}
                state.setdefault("tasks", []).append(item)
                state["task"] = body  # для старых читателей и отчёта
                return item

            item = store.mutate(apply)
            store.append_transcript("task_added", "human", task_id=item["id"], text=body[:200])
            print(f"task #{item['id']} added: {body}")
            still = [i["id"] for i in open_tasks(store.read())]
            if len(still) > 1:
                print(f"  open tasks now: {', '.join(str(i) for i in still)}")
        return 0

    if command == "tasks":
        current = store.read()
        path = write_tasks_md(store.root, current)
        print(f"# Журнал сессии -> {path}\n")
        print(render_tasks_md(current))
        return 0

    if command == "inbox":
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

    if command == "log":
        for entry in store.read_transcript(args.limit):
            print(json.dumps(entry, ensure_ascii=False))
        return 0

    if command == "all":
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

    if command == "notes":
        for path in sorted(store.notes_root.glob("*.md")):
            print(f"\n===== {path.name} =====")
            print(path.read_text(encoding="utf-8"))
        return 0

    if command == "reset":
        if not args.yes:
            print("refusing to reset without --yes", file=sys.stderr)
            return 2
        import shutil

        for path in (store.mailbox_root, store.notes_root, store.patches_root):
            shutil.rmtree(path, ignore_errors=True)
        store.state_path.unlink(missing_ok=True)
        store.transcript_path.unlink(missing_ok=True)
        fresh = Store(store.root)
        # Стирание состояния не должно повышать права. Fresh state по умолчанию
        # semi, то есть после wipe пара получала больше полномочий, чем до него, и
        # никто этого не просил. observe — тот уровень, который делает меньше
        # всего; повысить автономность оператор может явно.
        fresh.mutate(lambda st: st.__setitem__("autonomy", "observe"))
        store.append_transcript("reset", "human", autonomy="observe")
        print("state reset; autonomy is now 'observe' — raise it with "
              "`pairctl autonomy semi` if you want the pair to work on its own")
        return 0

    print(f"unknown command {command!r}", file=sys.stderr)
    return 2


def extract_state(argv: list[str]) -> tuple[Path, list[str]]:
    """Pull --state out of argv wherever it appears and resolve it.

    Subparser defaults silently overwrite same-named top-level options in
    argparse, so the option is removed from the grammar entirely and handled
    here instead. Both `server.py --state X ctl status` and
    `server.py ctl status --state X` work.
    """
    raw = ".pair"
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
    return Path(raw).expanduser().resolve(), rest


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pair-mailbox",
        description="MCP server and control tool for a two-agent collaboration.",
    )
    parser.add_argument("--role", help="This agent's role name. Fixed at configuration time.")

    sub = parser.add_subparsers(dest="ctl_command")
    sub.add_parser("status", help="show shared state")

    assign = sub.add_parser("assign", help="human: give write authority to a role")
    assign.add_argument("role")

    sub.add_parser("release", help="human: clear write authority")

    roster = sub.add_parser("roster", help="human: show who is registered, --prune to clean")
    roster.add_argument("--prune", action="store_true",
                        help="remove roles whose process is dead AND whose pane is gone")

    roles = sub.add_parser(
        "roles",
        help="human: pin who always writes and who always reviews, instead of racing each round",
    )
    roles.add_argument("--writer", help="role that always holds write authority")
    roles.add_argument("--critic", help="role that always reviews")
    roles.add_argument("--swap", action="store_true", help="exchange writer and critic")
    roles.add_argument("--clear", action="store_true", help="drop the pinning, go back to the race")
    roles.add_argument("--global", dest="global_", action="store_true",
                       help="also remember it for every project (PAIR_ROLES, default ~/.config/pair-mailbox/roles.json)")

    round_mode = sub.add_parser(
        "mode",
        help="human: choose what a round is for — 'fix' changes code, 'audit' does not",
    )
    round_mode.add_argument("mode", choices=list(MODES))

    autonomy = sub.add_parser(
        "autonomy",
        help="human: how far the pair may go without you — 'observe', 'semi' or 'auto'",
    )
    autonomy.add_argument("level", choices=list(AUTONOMIES))

    ok = sub.add_parser(
        "ok",
        help="human: say yes at the verify checkpoint, so the pair may close the round",
    )
    ok.add_argument("--revoke", action="store_true",
                    help="human: take the yes back, e.g. after looking at what changed since")

    task = sub.add_parser("task", help="human: add a task to the shared list")
    task.add_argument("text", nargs="?")
    task.add_argument("--drop", metavar="ID", default=None,
                      help="close task ID as dropped, with the reason below")
    task.add_argument("--done", metavar="ID", default=None,
                      help="close task ID as done")
    task.add_argument("--reason", default="",
                      help="why a task is dropped; required, an empty reason is refused")

    sub.add_parser("tasks", help="human: print the shared task list with statuses")

    inbox = sub.add_parser("inbox", help="show a role's pending messages")
    inbox.add_argument("role")

    log = sub.add_parser("log", help="print the audit trail")
    log.add_argument("--limit", type=int, default=0)

    sub.add_parser("notes", help="print shared notes")

    everywhere = sub.add_parser(
        "all", help="one row per project, across every project with a .pair"
    )
    everywhere.add_argument("paths", nargs="*", help="extra project or .pair directories")
    everywhere.add_argument("--stuck-min", type=int, default=30)
    everywhere.add_argument("--json", action="store_true")

    why = sub.add_parser("why", help="who writes, and the reason they gave")
    why.add_argument("paths", nargs="*", help="extra project or .pair directories")
    why.add_argument("--json", action="store_true")

    reset = sub.add_parser("reset", help="wipe shared state")
    reset.add_argument("--yes", action="store_true")

    return parser


def main(argv: list[str] | None = None) -> int:
    state_dir, rest = extract_state(list(sys.argv[1:] if argv is None else argv))
    args = build_parser().parse_args(rest)
    if args.ctl_command:
        return run_ctl(args, state_dir)
    if not args.role:
        print("error: --role is required when running as an MCP server", file=sys.stderr)
        return 2
    store = Store(state_dir)
    MCPServer(PairSession(args.role, store)).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())