"""Git Worktrees (S5): изоляция файловой системы для параллельных пар.

Функции:
- list_worktrees(repo_dir: Path) -> list[dict[str, Any]]
- setup_worktree(repo_dir: Path, branch: str, target_dir: Path | None = None) -> dict[str, Any]
- remove_worktree(repo_dir: Path, worktree_dir: Path, force: bool = False) -> bool
- prune_worktrees(repo_dir: Path) -> bool
- cmd_worktree(store, args) -> int (интеграция в pairctl)
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


def run_git(cmd: list[str], cwd: Path) -> tuple[int, str, str]:
    """Запустить git команду безопасно с перехватом stdout/stderr."""
    try:
        proc = subprocess.run(
            ["git"] + cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except OSError as err:
        return 1, "", str(err)


def list_worktrees(repo_dir: Path) -> list[dict[str, Any]]:
    """Получить список существующих git worktrees."""
    code, out, _ = run_git(["worktree", "list", "--porcelain"], repo_dir)
    if code != 0 or not out:
        return []

    worktrees: list[dict[str, Any]] = []
    current: dict[str, Any] = {}

    for line in out.splitlines():
        line = line.strip()
        if not line:
            if current and "worktree" in current:
                worktrees.append(current)
                current = {}
            continue
        if line.startswith("worktree "):
            current["worktree"] = line[9:].strip()
        elif line.startswith("HEAD "):
            current["head"] = line[5:].strip()
        elif line.startswith("branch "):
            current["branch"] = line[7:].strip()
        elif line == "bare":
            current["bare"] = True
        elif line == "detached":
            current["detached"] = True
        elif line.startswith("locked"):
            current["locked"] = True
        elif line.startswith("prunable"):
            current["prunable"] = True

    if current and "worktree" in current:
        worktrees.append(current)

    return worktrees


def setup_worktree(
    repo_dir: Path,
    branch: str,
    target_dir: Path | None = None,
) -> dict[str, Any]:
    """Создать или настроить worktree для указанной ветки.

    Если target_dir не указан, размещается в repo_dir / ".worktrees" / branch.
    """
    repo_dir = Path(repo_dir).resolve()
    if target_dir is None:
        target_dir = repo_dir / ".worktrees" / branch
    else:
        target_dir = Path(target_dir).resolve()

    target_dir.parent.mkdir(parents=True, exist_ok=True)

    # Проверим, не существует ли уже worktree по этому пути
    existing = list_worktrees(repo_dir)
    for wt in existing:
        if Path(wt.get("worktree", "")).resolve() == target_dir:
            return {
                "ok": True,
                "created": False,
                "worktree": str(target_dir),
                "branch": wt.get("branch", branch),
                "message": "worktree already exists",
            }

    # Проверим, существует ли локальная ветка
    code, _, _ = run_git(["rev-parse", "--verify", f"refs/heads/{branch}"], repo_dir)
    branch_exists = (code == 0)

    cmd = ["worktree", "add"]
    if not branch_exists:
        cmd.extend(["-b", branch, str(target_dir)])
    else:
        cmd.extend([str(target_dir), branch])

    code, out, err = run_git(cmd, repo_dir)
    if code != 0:
        return {
            "ok": False,
            "created": False,
            "worktree": str(target_dir),
            "branch": branch,
            "error": err or out or f"git worktree add failed with code {code}",
        }

    return {
        "ok": True,
        "created": True,
        "worktree": str(target_dir),
        "branch": branch,
        "message": "worktree created successfully",
    }


def remove_worktree(repo_dir: Path, worktree_dir: Path, force: bool = False) -> bool:
    """Удалить worktree через git worktree remove."""
    cmd = ["worktree", "remove", str(worktree_dir)]
    if force:
        cmd.append("--force")
    code, _, _ = run_git(cmd, repo_dir)
    return code == 0


def prune_worktrees(repo_dir: Path) -> bool:
    """Очистить устаревшие записи worktree."""
    code, _, _ = run_git(["worktree", "prune"], repo_dir)
    return code == 0


def cmd_worktree(store: Any, args: argparse.Namespace) -> int:
    """Обработчик подкоманды `pairctl worktree`."""
    subaction = getattr(args, "worktree_action", None) or "list"
    state_dir = Path(store.root).resolve()
    repo_dir = state_dir.parent

    # Проверяем, находимся ли мы в git репозитории
    code, top, _ = run_git(["rev-parse", "--show-toplevel"], repo_dir)
    if code == 0 and top:
        repo_dir = Path(top).resolve()

    if subaction == "list":
        wts = list_worktrees(repo_dir)
        if getattr(args, "json", False):
            print(json.dumps(wts, ensure_ascii=False, indent=2))
            return 0
        if not wts:
            print("No git worktrees found.")
            return 0
        for wt in wts:
            b = wt.get("branch", "detached")
            p = wt.get("worktree", "")
            print(f"- {b}: {p}")
        return 0

    elif subaction == "add":
        branch = getattr(args, "branch", None)
        if not branch:
            print("pairctl worktree add requires a branch name", file=sys.stderr)
            return 2
        res = setup_worktree(repo_dir, branch)
        if getattr(args, "json", False):
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            if res.get("ok"):
                print(f"Worktree for {branch} ready: {res.get('worktree')}")
            else:
                print(f"Failed to create worktree: {res.get('error')}", file=sys.stderr)
        return 0 if res.get("ok") else 1

    elif subaction == "prune":
        ok = prune_worktrees(repo_dir)
        if getattr(args, "json", False):
            print(json.dumps({"ok": ok}, ensure_ascii=False, indent=2))
        else:
            print("Pruned worktrees." if ok else "git worktree prune failed.")
        return 0 if ok else 1

    elif subaction == "remove":
        target = getattr(args, "target", None)
        if not target:
            print("pairctl worktree remove requires a path or branch", file=sys.stderr)
            return 2
        target_path = Path(target)
        if not target_path.is_absolute():
            # Попробуем найти среди существующих
            for wt in list_worktrees(repo_dir):
                if wt.get("branch") == target or wt.get("branch") == f"refs/heads/{target}":
                    target_path = Path(wt["worktree"])
                    break
                if Path(wt.get("worktree", "")).name == target:
                    target_path = Path(wt["worktree"])
                    break
        ok = remove_worktree(repo_dir, target_path, force=getattr(args, "force", False))
        if getattr(args, "json", False):
            print(json.dumps({"ok": ok}, ensure_ascii=False, indent=2))
        else:
            print(f"Removed worktree {target_path}." if ok else "Failed to remove worktree.")
        return 0 if ok else 1

    print(f"Unknown worktree action: {subaction}", file=sys.stderr)
    return 2
