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

    # Вердикт должен быть одобрительным (kind == 'verdict')
    # Проверяем, не reject ли это
    v_rev = last_verdict.get("rev") or {}
    v_kind = last_verdict.get("kind")
    if v_kind != "verdict":
        return False, f"last review note is not a verdict (kind={v_kind!r})"

    # Проверка свежести через единый канонический предикат review.is_stale
    cur_dirty = review.dirty_files(repo_dir)
    changed = review.changed_between(repo_dir, str(v_rev.get("head") or ""), head)
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
