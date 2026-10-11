"""Привязка ревью к ревизии и лимит очереди (review-queue-and-stale-verdicts).

Вердикт критика относится к конкретному состоянию кода, а не к задаче
вообще: HEAD плюс хэш незакоммиченного diff. Писатель продолжает коммитить,
не дожидаясь, поэтому вердикт, применённый дословно к уехавшей ревизии,
создаёт видимость проверенности на непроверенном.

Правила (согласованы, карточка в docs/backlog/):
- stale только при пересечении файлов (dirty overlap): ревьюил composer.py,
  а писатель ушёл в scripts/file-size-gate.sh — вердикт не stale;
- писатель не блокируется в середине работы; блокировка — перед done:
  очередь висящих ревью пуста и нет stale-вердиктов;
- лимит очереди: не более 2 неподтверждённых запросов; на третьем —
  остановиться и войти в pair_read.

Всё, что касается git, — fail-open: нет репозитория или команда не
ответила — не stale и не блокировка. Иначе окружение без git (или с
битым git) навсегда закрывало бы раунд.
"""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Any

MAX_OPEN_REVIEWS = 2


def _run_git(root: Path, *args: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=str(root),
            capture_output=True, text=True, timeout=5,
        )
    except Exception:  # noqa: BLE001 - git недоступен, это не приговор раунду
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def git_head(root: Path) -> str:
    out = _run_git(root, "rev-parse", "HEAD")
    return (out or "").strip()


def dirty_files(root: Path, include_untracked: bool = False) -> list[str]:
    """Файлы с незакоммиченными изменениями.

    По умолчанию возвращает только отслеживаемые (tracked) изменения (M, D, A, R).
    Строки untracked (??) игнорируются, если include_untracked не установлен в True.
    """
    out = _run_git(root, "status", "--porcelain")
    if out is None:
        return []
    files: list[str] = []
    for line in out.splitlines():
        line = line.rstrip()
        if not line:
            continue
        # Формат: XY <path> или XY <old> -> <new> при переименовании.
        status_prefix = line[:2]
        if not include_untracked and status_prefix == "??":
            continue
        path = line[3:] if len(line) > 3 else ""
        if " -> " in path:
            path = path.split(" -> ")[-1].strip()
        path = path.strip().strip('"')
        if path:
            files.append(path)
    return sorted(set(files))


def dirty_hash(root: Path) -> str:
    out = _run_git(root, "diff", "--no-ext-diff", "HEAD", "--")
    if out is None:
        return ""
    return hashlib.sha256(out.encode("utf-8")).hexdigest()[:16]


def head_files(root: Path, head: str) -> list[str]:
    """Файлы, затронутые коммитом head (для чистого ревью без dirty)."""
    if not head:
        return []
    out = _run_git(root, "show", "--pretty=format:", "--name-only", head)
    if out is None:
        return []
    return sorted({ln.strip() for ln in out.splitlines() if ln.strip()})


def changed_between(root: Path, old: str, new: str) -> list[str]:
    if not old or not new or old == new:
        return []
    out = _run_git(root, "diff", "--name-only", f"{old}..{new}", "--")
    if out is None:
        return []
    return sorted({ln.strip() for ln in out.splitlines() if ln.strip()})


def snapshot(root: Path) -> dict[str, Any]:
    """Снимок ревизии для привязки запроса/вердикта.

    У1: Покрытие объединяет файлы коммита и реальные tracked-изменения
    (head_files ∪ tracked-dirty), чтобы наличие dirty файлов не вытесняло
    файлы коммита из покрытия вердикта.
    """
    head = git_head(root)
    dirty = dirty_files(root, include_untracked=False)
    h_files = head_files(root, head)
    if h_files:
        files = sorted(set(h_files) | set(dirty))
    else:
        files = dirty
    return {"head": head, "dirty": dirty_hash(root), "files": files}


def is_stale(old: dict[str, Any], current_head: str,
             changed_since: list[str], current_dirty: list[str]) -> tuple[bool, str]:
    """Устарел ли вердикт/снимок old относительно текущего состояния.

    Stale — только если ревизия уехала И пересечение по файлам есть.
    Пустые old['files'] означают «покрытие неизвестно» — тогда любая смена
    HEAD считается stale (чистое ревью коммита без списка файлов).
    Без git (current_head пуст) — никогда не stale: fail-open.
    """
    if not current_head:
        return False, "нет HEAD для сравнения"
    old_head = str(old.get("head") or "")
    if not old_head:
        return False, "у запроса нет привязки к ревизии"
    if old_head == current_head and not current_dirty:
        return False, "ревизия не уехала"
    old_files = set(old.get("files") or [])
    if not old_files:
        if old_head != current_head:
            return True, f"HEAD уехал {old_head[:8]}..{current_head[:8]} без списка файлов"
        return False, "HEAD тот же"
    touched = set(changed_since) | set(current_dirty)
    overlap = sorted(old_files & touched)
    if overlap:
        return True, f"затронуты те же файлы: {', '.join(overlap[:5])}"
    return False, "последующие изменения те же файлы не затронули"


def open_count(obligations_root: Path, frm: str) -> int:
    """Сколько неподтверждённых запросов висит у автора frm."""
    import json

    if not obligations_root.is_dir():
        return 0
    count = 0
    for path in obligations_root.glob("*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if record.get("from") == frm:
            count += 1
    return count


def queue_full(obligations_root: Path, frm: str,
               limit: int = MAX_OPEN_REVIEWS) -> bool:
    return open_count(obligations_root, frm) >= limit


def pin_verdict(store: Any, role: str, kind: str, text: str, utcnow: Any) -> dict[str, Any]:
    """Привязать вердикт к ревизии: HEAD + dirty-hash + файлы.

    Вынесено из server.tool_pair_note (server-py-still-a-monolith): привязка
    живёт рядом со snapshot, который её строит. Молчаливое применение вердикта
    к уехавшей ревизии хуже отсутствия вердикта. Fail-open: без git пишется
    пустой снимок, снимок не роняет вердикт.
    """
    rev = snapshot(store.project_root)

    def remember_verdict(state: dict[str, Any]) -> None:
        verdicts = state.setdefault("verdicts", [])
        verdicts.append({"role": role, "kind": kind,
                         "at": utcnow(), "rev": rev,
                         "chars": len(text)})
        del verdicts[: max(0, len(verdicts) - 20)]
    store.mutate(remember_verdict)
    store.append_transcript("verdict_pinned", role,
                            note_kind=kind, head=(rev.get("head") or "")[:12])
    return rev
