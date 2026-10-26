"""Git Worktrees (S5): filesystem isolation for parallel pairs.

Functions:
- list_worktrees(repo_dir: Path) -> list[dict[str, Any]]
- setup_worktree(repo_dir: Path, branch: str, target_dir: Path | None = None) -> dict[str, Any]
- remove_worktree(repo_dir: Path, worktree_dir: Path, force: bool = False) -> bool
- prune_worktrees(repo_dir: Path) -> bool
- cmd_worktree(store, args) -> int (pairctl integration)
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
    "Run a git command safely capturing stdout/stderr."
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
    "List the existing git worktrees."
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
    visa: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a worktree that ISOLATES A TASK of the running round.

    With no target_dir given, lands in repo_dir / ".worktrees" / branch.

    `visa` is the AGREED isolation strategy of the round that asked for this
    worktree — the result of `isolation.agreed_strategy`. It is required, and
    the default is refusal, because the backlog position (P1) says a worktree
    must not be created before the critic has signed the plan that asked for
    it. Fail-closed at the API level: a caller that forgets to pass the visa
    gets a refusal, never a worktree nobody agreed to.

    The visa also says WHICH strategy was agreed. A plan that chose `in-place`
    (no worktree needed) does not authorise one: the refusal names that.

    For the LAUNCH environment of a pair — a different thing, see
    `bootstrap_worktree` — use that function instead.
    """
    if not visa or not visa.get("ok"):
        why = (visa or {}).get("why") or (
            "no agreed isolation strategy for this round: the critic must sign "
            "the round plan before a worktree is created")
        return {"ok": False, "created": False, "branch": branch,
                "error": f"worktree creation refused: {why}"}
    if visa.get("mode") == "in-place":
        return {"ok": False, "created": False, "branch": branch,
                "error": ("worktree creation refused: the agreed strategy for this "
                          "round is `in-place` — no worktree was asked for")}

    return _create_worktree(repo_dir, branch, target_dir)


def bootstrap_worktree(
    repo_dir: Path,
    branch: str,
    target_dir: Path | None = None,
) -> dict[str, Any]:
    """Create the LAUNCH environment of a pair: the room the round happens in.

    This is a different operation from `setup_worktree`, and the difference is
    not a loophole but the thing the gate is about:

    - `bootstrap_worktree` runs BEFORE a round exists. There is no round, no
      plan, no critic to sign anything, and no task whose isolation is being
      decided. The decision here is the HUMAN's: they typed `pairup
      --worktree` or `pair-duo --worktree` on the command line.
    - `setup_worktree` runs DURING a round, to isolate one of the round's
      tasks. That decision belongs to the writer, and the critic signs it.

    The git work is the same; only the gate differs, and the gate differs
    because the question differs. The round that starts inside this worktree
    still declares its own isolation strategy in its plan, and every
    ADDITIONAL worktree it needs still passes `setup_worktree`.
    """
    return _create_worktree(repo_dir, branch, target_dir)


def _create_worktree(
    repo_dir: Path,
    branch: str,
    target_dir: Path | None = None,
) -> dict[str, Any]:
    """The git work behind both entry points: create or reuse the worktree."""
    repo_dir = Path(repo_dir).resolve()
    if target_dir is None:
        target_dir = repo_dir / ".worktrees" / branch
    else:
        target_dir = Path(target_dir).resolve()

    target_dir.parent.mkdir(parents=True, exist_ok=True)

    # Checking whether a worktree already stands at this path
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

    # Checking whether the local branch exists
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
    "Remove a worktree over git worktree remove."
    cmd = ["worktree", "remove", str(worktree_dir)]
    if force:
        cmd.append("--force")
    code, _, _ = run_git(cmd, repo_dir)
    return code == 0


def prune_worktrees(repo_dir: Path) -> bool:
    "Prune stale worktree records."
    code, _, _ = run_git(["worktree", "prune"], repo_dir)
    return code == 0


def _current_visa(state_dir: Path) -> dict[str, Any]:
    """The agreed isolation strategy of the round that is running now.

    The state dir holds `state.json` with the round and the writer. When it
    cannot be read, the answer is a refusal — not a guess. A worktree created
    on a guess is exactly what this gate exists to prevent.
    """
    try:
        from isolation import agreed_strategy
    except ImportError:  # pragma: no cover - isolation sits next in the package
        return {"ok": False, "why": "the isolation module is unavailable"}
    try:
        state = json.loads((Path(state_dir) / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"ok": False, "why": f"state.json unreadable: {exc}"}
    round_number = int(state.get("round", 0) or 0)
    writer = str(state.get("writer") or "")
    return agreed_strategy(state_dir, round_number, writer)


def cleanup_worktree_for_branch(repo_dir: Path, branch: str) -> dict[str, Any]:
    """Remove the worktree that held `branch`, then prune the records.

    The worktree a merged round branch lived in has no reason to outlive the
    merge: the commits are in the trunk, the branch is deleted by the merge
    itself (`--delete-branch`), and a directory left behind is garbage on the
    developer's disk. §19 says the merged round branch dies by construction —
    this makes the DIRECTORY die with it.

    The main worktree is never touched: it is the repository itself, and
    `git worktree remove` on it would be a catastrophe, not a cleanup.

    Returns {ok, removed, path, pruned, why}. `ok` is False when there was
    nothing to clean — that is not a failure, it is the normal case for a
    round that worked in place.
    """
    repo_dir = Path(repo_dir).resolve()
    wanted = branch.strip()
    if wanted.startswith("refs/heads/"):
        wanted = wanted[len("refs/heads/"):]
    main = None
    target = None
    for wt in list_worktrees(repo_dir):
        path = Path(wt.get("worktree", ""))
        if not path.exists():
            continue
        if main is None:
            main = path.resolve()
        wt_branch = str(wt.get("branch", "") or "")
        if wt_branch.startswith("refs/heads/"):
            wt_branch = wt_branch[len("refs/heads/"):]
        if wt_branch == wanted and path.resolve() != main:
            target = path
            break
    if target is None:
        return {"ok": True, "removed": False, "path": None, "pruned": False,
                "why": f"no worktree held the branch {branch!r}"}
    removed = remove_worktree(repo_dir, target, force=True)
    pruned = prune_worktrees(repo_dir)
    if not removed:
        return {"ok": False, "removed": False, "path": str(target),
                "pruned": pruned,
                "why": f"git worktree remove failed for {target}"}
    return {"ok": True, "removed": True, "path": str(target), "pruned": pruned,
            "why": ""}


def cmd_worktree(store: Any, args: argparse.Namespace) -> int:
    "The `pairctl worktree` subcommand handler."
    subaction = getattr(args, "worktree_action", None) or "list"
    state_dir = Path(store.root).resolve()
    repo_dir = state_dir.parent

    # Checking whether we stand inside a git repo
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
        # The agreed strategy of the current round: the critic's signed plan.
        visa = _current_visa(state_dir)
        res = setup_worktree(repo_dir, branch, visa=visa)
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
            # Trying to find among the existing ones
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
