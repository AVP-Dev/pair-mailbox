"""Машинный гейт слияния PR и Feature Freeze (раунд 60, задача #67).

Модуль реализует строгую машинную защиту от слияния PR до завершения ревью:
1. `check_pr_merge(store: Store, repo_dir: Path) -> tuple[bool, str]`:
   - Отказ, если текущая фаза не `verify` (запрещено сливать в scoping, plan, implement, done).
   - Отказ, если отсутствует git-репозиторий (fail-closed на пути слияния).
   - Отказ, если в раунде нет успешного test evidence (`kind="test"`, `ok=True`).
   - Отказ, если нет вердикта критика или последний вердикт не одобрен (reject / нет approve).
   - Запрет self-approval: отказ, если вердикт оставлен писателем (`role == store.writer`).
   - Проверка свежести: прямое переиспользование `review.is_stale(...)`. Любой новый коммит
     или незакоммиченное изменение файлов делает вердикт stale и блокирует слияние.
2. `cmd_pr(store: Store, args: argparse.Namespace) -> int`:
   - CLI-команда `pairctl pr merge [pr_id] [--force] [--reason REASON]`.
   - Флаг `--force`: требует непустой `--reason`, фиксируется в транскрипте как `pr_merge_forced`.
   - При успешной проверке вызывает `gh pr merge <pr_id> --squash --delete-branch`.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Any

import review


def check_pr_merge(store: Any, repo_dir: Path) -> tuple[bool, str]:
    """Проверить, разрешено ли слияние PR по правилам протокола.

    Возвращает (True, "") при успешной проверке или (False, причина_отказа).
    """
    head = review.git_head(repo_dir)
    if not head:
        return False, "cannot verify: not a git repo"

    state = store.read()
    phase = state.get("phase")
    if phase != "verify":
        return False, f"phase is {phase!r}, PR merge allowed strictly in 'verify'"

    # Проверка наличия успешного test evidence в текущем раунде
    cur_round = int(state.get("round", 0))
    round_ev = [
        ev for ev in (state.get("evidence") or [])
        if int(ev.get("round", -1)) == cur_round
    ]
    has_test_ok = any(
        ev.get("kind") == "test" and ev.get("ok")
        for ev in round_ev
    )
    if not has_test_ok:
        return False, f"round {cur_round} has no passing test evidence (kind='test', ok=True)"

    # Проверка вердикта критика
    verdicts = state.get("verdicts") or []
    if not verdicts:
        return False, "no verdict found: peer review approval is required before merge"

    last_verdict = verdicts[-1]
    author = last_verdict.get("role")
    writer = state.get("writer")

    # Запрет self-approval: писатель не может сам себе одобрить PR
    if writer and author == writer:
        return False, f"self-approval forbidden: verdict author {author!r} is current writer"

    v_rev = last_verdict.get("rev") or {}
    v_kind = last_verdict.get("kind")
    if v_kind != "verdict":
        return False, f"last review note is not a verdict (kind={v_kind!r})"

    # Вердикт обязан иметь привязку к ревизии (head)
    v_head = str(v_rev.get("head") or "").strip()
    if not v_head:
        return False, "verdict has no revision binding (rev.head is empty)"

    # Проверка текста последнего вердикта из notes/verdict.md (fail-closed: файл обязан существовать)
    notes_dir = store.notes_root
    verdict_file = notes_dir / "verdict.md"
    if not verdict_file.is_file():
        return False, "notes/verdict.md does not exist: approval text cannot be verified"

    import re
    try:
        content = verdict_file.read_text(encoding="utf-8")
        blocks = [b for b in content.split("\n## ") if b.strip()]
        if not blocks:
            return False, "notes/verdict.md is empty: no verdict text found"
        last_block = blocks[-1]
        # Д1: Машинный канал одобрения — строго маркер verdict-approve <head|round>, без ложных срабатываний прозы
        VERDICT_APPROVE_RE = re.compile(
            r"^\s*verdict-approve\s+(?:round\s+(\d+)|([0-9a-f]{7,40}))\b",
            re.IGNORECASE | re.MULTILINE,
        )
        # Отказная ветка толерантна к прозе (ложный отказ безопасен)
        VERDICT_REJECT_RE = re.compile(
            r"(?:\bverdict-reject\b|ВЕРДИКТ\b[^\n]*\b(?:ОТКАЗ|REJECT|CHANGES REQUESTED)\b|\bне\s+одобрен[оаы]?\b)",
            re.IGNORECASE,
        )
        if VERDICT_REJECT_RE.search(last_block):
            return False, "last verdict rejected the changes (ОТКАЗ / REJECT / НЕ ОДОБРЕНО)"

        approve_match = VERDICT_APPROVE_RE.search(last_block)
        if not approve_match:
            return False, "last verdict is not approved (no structured 'verdict-approve <head|round>' marker)"

        # Д2: Сверка значения маркера с вердиктом (v_head) или текущим HEAD / текущим раундом
        m_round, m_head = approve_match.groups()
        if m_round:
            cur_round = str(state.get("round", ""))
            if str(m_round) != cur_round:
                return False, f"marker round '{m_round}' does not match current round '{cur_round}'"
        elif m_head:
            m_head_lower = m_head.lower()
            head_lower = head.lower()
            v_head_lower = v_head.lower()
            matches_head = head_lower.startswith(m_head_lower) or m_head_lower.startswith(head_lower)
            matches_v_head = v_head_lower.startswith(m_head_lower) or m_head_lower.startswith(v_head_lower)
            if not (matches_head or matches_v_head):
                return False, f"marker head '{m_head}' does not match current HEAD '{head}'"
    except OSError as err:
        return False, f"failed to read notes/verdict.md: {err}"

    # У3: Защита пути слияния — отказ при любом грязном дереве (tracked или untracked).
    # Слияние разрешено строго на чистом закоммиченном дереве («сливаем то, что проверено и закоммичено»).
    uncommitted = review.dirty_files(repo_dir, include_untracked=True)
    try:
        rel_notes = str(notes_dir.resolve().relative_to(repo_dir.resolve()))
    except (ValueError, AttributeError):
        rel_notes = None
    if rel_notes:
        rel_notes_slash = rel_notes.rstrip("/") + "/"
        uncommitted = [
            f for f in uncommitted
            if f != rel_notes and not f.startswith(rel_notes_slash) and f != ".pair" and not f.startswith(".pair/")
        ]
    else:
        uncommitted = [f for f in uncommitted if f != ".pair" and not f.startswith(".pair/")]
    if uncommitted:
        return False, f"working tree has uncommitted changes ({len(uncommitted)} dirty files): {', '.join(uncommitted[:3])}"

    # Проверка свежести через единый канонический предикат review.is_stale
    cur_dirty = review.dirty_files(repo_dir)
    changed = review.changed_between(repo_dir, v_head, head)
    stale, reason = review.is_stale(v_rev, head, changed, cur_dirty)
    if stale:
        return False, f"verdict is stale: {reason}"

    return True, ""


def run_gh_merge(repo_dir: Path, pr_id: str | None) -> tuple[int, str]:
    """Запустить слияние через gh cli."""
    cmd = ["gh", "pr", "merge"]
    if pr_id:
        cmd.append(pr_id)
    cmd.extend(["--squash", "--delete-branch"])
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=30,
        )
        output = (proc.stdout + "\n" + proc.stderr).strip()
        return proc.returncode, output
    except OSError as err:
        return 1, f"failed to execute gh: {err}"
    except subprocess.TimeoutExpired:
        return 1, "gh pr merge timed out"


def cmd_pr(store: Any, args: argparse.Namespace) -> int:
    """Обработчик подкоманд `pairctl pr`."""
    action = getattr(args, "pr_action", None)
    if action != "merge":
        print(f"unknown pr action: {action!r}", file=sys.stderr)
        return 2

    repo_dir = store.project_root
    force = bool(getattr(args, "force", False))
    reason = str(getattr(args, "reason", "") or "").strip()

    if force:
        if not reason:
            print("error: --force requires --reason with explanation", file=sys.stderr)
            return 2
        # Остаточное требование критика: обход не доступен писателю в одиночку,
        # требуется предварительное одобрение человеком (pairctl ok на текущий раунд)
        from server import human_ok_for_round
        state = store.read()
        if not human_ok_for_round(state):
            print("PR merge gate REFUSED: --force requires human authority (pairctl ok for current round)", file=sys.stderr)
            return 1
        store.append_transcript(
            "pr_merge_forced",
            getattr(args, "role", "") or "human",
            reason=reason,
            pr_id=getattr(args, "pr_id", None),
        )
    else:
        ok, why_not = check_pr_merge(store, repo_dir)
        if not ok:
            print(f"PR merge gate REFUSED: {why_not}", file=sys.stderr)
            return 1

    pr_id = getattr(args, "pr_id", None)
    code, out = run_gh_merge(repo_dir, pr_id)
    if code != 0:
        print(f"gh pr merge failed (exit {code}):\n{out}", file=sys.stderr)
        return code

    print(f"PR merge successful: {out}")
    store.append_transcript(
        "pr_merged",
        getattr(args, "role", "") or "writer",
        pr_id=pr_id,
        forced=force,
    )
    return 0
