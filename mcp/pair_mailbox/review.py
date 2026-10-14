"""Binding reviews to revisions plus a queue limit (review-queue-and-stale-verdicts).

A critic verdict covers a concrete code state, never the task at large: HEAD plus the
uncommitted-diff hash. The writer keeps committing with no wait, so a verdict applied
verbatim to a moved revision fakes coverage on the uncovered.

Rules (agreed, card in docs/backlog/):
- stale only on file overlap (dirty overlap): reviewed composer.py while the writer
  moved into scripts/file-size-gate.sh — the verdict stays fresh;
- the writer never blocks mid-work; the block stands before done: the pending-review
  queue is empty with no stale verdicts;
- queue limit: no more than 2 unacked requests; on the third stop and enter pair_read.

All git touches run fail-open: no repo or a dead command means neither stale nor
block. Or a gitless env (or a broken git) would close the round forever.
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
    except Exception:  # noqa: BLE001 - git unavailable, no round sentence
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def git_head(root: Path) -> str:
    out = _run_git(root, "rev-parse", "HEAD")
    return (out or "").strip()


def dirty_files(root: Path, include_untracked: bool = False) -> list[str]:
    """Files with uncommitted changes.

    By default only tracked changes return (M, D, A, R). Untracked (??) lines skip
    unless include_untracked sets True.
    """
    out = _run_git(root, "status", "--porcelain")
    if out is None:
        return []
    files: list[str] = []
    for line in out.splitlines():
        line = line.rstrip()
        if not line:
            continue
        # Format: XY <path> or XY <old> -> <new> on rename.
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
    "Files touched by the head commit (for clean review with no dirty)."
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
    """A revision snapshot binding a request/verdict.

    U1: coverage unions commit files with real tracked-dirty ones (head_files ∪
    tracked-dirty), so dirty-file presence never pushes commit files out of the
    verdict coverage.
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
    """Whether verdict/snapshot old went stale against current state.

    Stale only when the revision moved AND file overlap stands. Empty old[files]
    means "coverage unknown" — then any HEAD move counts stale (a clean commit
    review with no file list). With no git (empty current_head) never stale:
    fail-open.
    """
    if not current_head:
        return False, "no HEAD to compare against"
    old_head = str(old.get("head") or "")
    if not old_head:
        return False, "the request owns no revision binding"
    if old_head == current_head and not current_dirty:
        return False, "the revision never moved"
    old_files = set(old.get("files") or [])
    if not old_files:
        if old_head != current_head:
            return True, f"HEAD moved {old_head[:8]}..{current_head[:8]} with no file list"
        return False, "same HEAD"
    touched = set(changed_since) | set(current_dirty)
    overlap = sorted(old_files & touched)
    if overlap:
        return True, f"the same files touched: {', '.join(overlap[:5])}"
    return False, "later changes never touched the same files"


def open_count(obligations_root: Path, frm: str) -> int:
    "How many unacked requests hang at author frm."
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
    """Bind the verdict to the revision: HEAD + dirty-hash + files.

    Moved out of server.tool_pair_note (server-py-still-a-monolith): the binding
    lives beside the snapshot building it. Silently applying a verdict to a moved
    revision reads worse than no verdict. Fail-open: with no git an empty snapshot
    writes, never dropping the verdict.
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
