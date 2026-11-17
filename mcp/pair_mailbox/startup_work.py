"""Find resumable project work before ``pairup`` starts either agent."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import TextIO

from store_core import Store
import worktree


def changed_paths(root: Path) -> list[str]:
    """Return tracked and untracked changes, preserving unusual path names."""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            cwd=root, capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError(f"pairup: cannot inspect worktree changes in {root}: {error}") from error
    if result.returncode:
        if "not a git repository" in result.stderr.lower():
            return []
        detail = result.stderr.strip() or f"exit {result.returncode}"
        raise ValueError(f"pairup: git status failed in {root}: {detail}")
    rows = result.stdout.split("\0")
    paths: list[str] = []
    index = 0
    while index < len(rows):
        row = rows[index]
        index += 1
        if not row:
            continue
        state = row[:2]
        path = row[3:] if len(row) >= 4 else row
        if (path.startswith(".pair/") or path == ".pair"
                or path.startswith(".worktrees/")):
            continue
        # New-project bootstrap copies this shared policy template. It is not
        # user work; a later tracked edit still appears as a normal modification.
        if state == "??" and path == "anti-mock.json":
            continue
        if path:
            paths.append(path)
        if state.strip() and any(mark in state for mark in "RC"):
            # Porcelain v1 emits the destination path as the next NUL field.
            if index < len(rows) and rows[index]:
                paths.append(rows[index])
                index += 1
    return sorted(set(paths))


def candidate_worktrees(project_root: Path) -> list[dict]:
    """Inspect every registered checkout, plus the root if Git omitted it."""
    paths = [Path(item["worktree"]).resolve()
             for item in worktree.list_worktrees(project_root)
             if item.get("worktree") and Path(item["worktree"]).exists()]
    root = project_root.resolve()
    if root.exists() and root not in paths:
        paths.append(root)
    result = []
    for path in paths:
        files = changed_paths(path)
        if not files:
            continue
        meta = next((item for item in worktree.list_worktrees(project_root)
                     if Path(item.get("worktree", "")).resolve() == path), {})
        result.append({
            "workspace": str(path),
            "branch": meta.get("branch", ""),
            "files": files,
        })
    return result


def build_candidates(state: dict, project_root: Path) -> list[dict]:
    """Open tasks are always resumable; completed tasks surface when code is dirty."""
    dirty = candidate_worktrees(project_root)
    round_number = int(state.get("round") or 0)
    tasks = [item for item in state.get("tasks", []) if isinstance(item, dict)]
    candidates: list[dict] = []

    for task in tasks:
        status = str(task.get("status") or "")
        current_round = int(task.get("round") or 0) == round_number
        if status != "open" and not (dirty and current_round and status == "done"):
            continue
        workspaces = dirty or [{"workspace": "", "branch": "", "files": []}]
        for workspace in workspaces:
            task_id = int(task.get("id") or 0)
            candidates.append({
                "key": f"task:{task_id}:{workspace['workspace']}",
                "task_id": task_id,
                "status": status,
                "text": str(task.get("text") or "").strip(),
                **workspace,
            })

    # A dirty checkout remains selectable even when no task card can be safely
    # associated with it. It is shown as workspace recovery, never guessed away.
    for workspace in dirty:
        candidates.append({
            "key": f"worktree:{workspace['workspace']}",
            "task_id": None,
            "status": "uncommitted changes",
            "text": "Resume this worktree",
            **workspace,
        })
    return candidates


def _terminal_text(value: object) -> str:
    """Escape terminal controls in task text and Git-controlled path names."""
    escaped = []
    for char in str(value):
        if char.isprintable():
            escaped.append(char)
            continue
        code = ord(char)
        escaped.append(f"\\x{code:02x}" if code <= 0xff else
                       f"\\u{code:04x}" if code <= 0xffff else f"\\U{code:08x}")
    return "".join(escaped)


def _describe(candidate: dict, number: int) -> str:

    task_id = candidate.get("task_id")
    subject = (f"#{task_id} [{candidate['status']}] {candidate['text']}"
               if task_id is not None else candidate["text"])
    branch = candidate.get("branch") or Path(candidate["workspace"]).name or "project checkout"
    lines = [f"  {number}) {_terminal_text(subject)}",
             f"     worktree: {_terminal_text(branch)}"]
    if candidate.get("files"):
        lines.append("     changed files:")
        lines.extend(f"       - {_terminal_text(path)}" for path in candidate["files"])
    return "\n".join(lines)


def _read_line(prompt: str, stdin: TextIO, stderr: TextIO) -> str:
    stderr.write(prompt)
    stderr.flush()
    return stdin.readline().strip()


def choose_work(state: dict, project_root: Path, *, resume: str = "",
                start_new: bool = False, task_text: str = "",
                interactive: bool = False, stdin: TextIO | None = None,
                stderr: TextIO | None = None) -> dict:
    """Select resume/new before agent launch; a non-TTY cannot silently skip work."""
    stdin = stdin or sys.stdin
    stderr = stderr or sys.stderr
    candidates = build_candidates(state, project_root)

    if task_text:
        return {"action": "new", "task": task_text}
    if resume:
        matches = [item for item in candidates
                   if str(item.get("task_id")) == str(resume) or item["key"] == resume]
        if len(matches) != 1:
            reason = "no resumable item matches" if not matches else "resume item is ambiguous"
            raise ValueError(f"pairup: {reason} {resume!r}; choose from the displayed candidates")
        return {"action": "resume", **matches[0]}

    if candidates:
        stderr.write(f"Resumable work in {_terminal_text(project_root.resolve())}:\n")
        for number, candidate in enumerate(candidates, 1):
            stderr.write(_describe(candidate, number) + "\n")
        stderr.write(f"  {len(candidates) + 1}) Start a new task\n")
        if not interactive:
            task_candidates = [item for item in candidates if item.get("task_id") is not None]
            task_ids = {item["task_id"] for item in task_candidates}
            if (len(task_ids) == 1 and task_candidates
                    and all(item["status"] == "open" for item in task_candidates)):
                workspaces = {item["workspace"] for item in task_candidates}
                if len(workspaces) <= 1:
                    # A headless repeat launch can safely continue one explicit
                    # open task; it must stop for closed-but-dirty recovery or
                    # whenever more than one task/worktree needs human choice.
                    return {"action": "resume", **task_candidates[0]}
            raise ValueError(
                "pairup: resumable work needs a choice; use --resume ID, --new --task TEXT, "
                "or run from a terminal")
        selected = _read_line("Choose a number: ", stdin, stderr)
        try:
            number = int(selected)
        except ValueError:
            raise ValueError("pairup: enter a candidate number") from None
        if 1 <= number <= len(candidates):
            return {"action": "resume", **candidates[number - 1]}
        if number != len(candidates) + 1:
            raise ValueError("pairup: selection is outside the displayed list")
        start_new = True
    if start_new:
        if not interactive:
            raise ValueError("pairup: --new needs --task TEXT when no terminal is available")
        task_text = _read_line("New task: ", stdin, stderr)
        if not task_text:
            raise ValueError("pairup: a new task needs a description")
        return {"action": "new", "task": task_text}
    return {"action": "none"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--resume", default="")
    parser.add_argument("--new", action="store_true")
    parser.add_argument("--task", default="")
    parser.add_argument("--interactive", action="store_true")
    args = parser.parse_args(argv)
    try:
        state = Store(Path(args.state)).read()
        choice = choose_work(
            state, Path(args.root), resume=args.resume, start_new=args.new,
            task_text=args.task, interactive=args.interactive,
        )
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(choice, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
