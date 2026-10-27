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
import hashlib
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


#: The caches and dependencies a worktree SHARES with the main worktree.
#:
#: A closed list, not a glob: a glob over the tree would share source files
#: and tracked data along with the caches, and the worktree would then be
#: editing the main checkout's files through a symlink. These names are
#: directories that are rebuilt from the project's own sources, so sharing
#: them shares nothing but the cost of rebuilding.
DEFAULT_SHARED = [
    ".venv",
    "node_modules",
    ".pytest_cache",
    "__pycache__",
    "dist",
    "build",
]

#: The project setting that overrides the list.
SHARE_SETTING = "worktree-share"

#: The durable worktree lifecycle journal: `.pair/worktree-log.jsonl`.
#:
#: It is NOT the transcript. The transcript rotates (four archives, the oldest
#: deleted), and a rotated-away record is indistinguishable from a record that
#: never existed. The lifecycle of a worktree outlives the room that created
#: it: the directory is removed with `git worktree remove --force`, the branch
#: is deleted by the merge, and the squash merge replaces the branch's commits
#: with one new sha on the trunk. Everything that says "that worktree existed,
#: held these commits, and died at this merge" must therefore live somewhere
#: that is never rotated and never removed with the worktree.
WORKTREE_LOG_FILE = "worktree-log.jsonl"

#: The lifecycle events the journal knows.
JOURNAL_EVENTS = ("created", "committed", "merged", "removed")


def worktree_journal_path(state_dir: Path | str) -> Path:
    """Where the durable journal lives: inside the state dir, never rotated."""
    return Path(state_dir) / WORKTREE_LOG_FILE


def worktree_journal_append(state_dir: Path | str, event: str,
                             **fields: Any) -> dict[str, Any]:
    """Append one lifecycle event to the durable journal.

    The journal is append-only JSONL: one JSON object per line, so a torn
    write at the end of the file costs at most the last line, and every
    earlier line is still readable. A journal that is rewritten whole is a
    journal that can lose everything the moment the write fails.

    The write is append-only and O(1) per event: a worktree lifecycle is
    short and the journal is read linearly when asked.

    Returns the record that was written. An unreadable or unwritable journal
    raises — the caller decides whether that is fatal, and a caller that
    swallows it is the reason the record goes missing.
    """
    if event not in JOURNAL_EVENTS:
        raise ValueError(
            f"unknown worktree journal event {event!r}: "
            f"one of {', '.join(JOURNAL_EVENTS)}")
    from store_core import utcnow
    record: dict[str, Any] = {"event": event, "at": utcnow()}
    for key, value in fields.items():
        if value is None:
            continue
        record[key] = value
    path = worktree_journal_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
    return record


def worktree_journal_read(state_dir: Path | str) -> list[dict[str, Any]]:
    """Every journal record, oldest first.

    A line that cannot be parsed is SKIPPED, not fatal: a journal that a
    single torn line makes unreadable is worse than a journal with one hole
    in it. The skipped line is the honest cost of appending without a
    transaction, and the rest of the trail is still evidence.
    """
    path = worktree_journal_path(state_dir)
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            records.append(row)
    return records


def shared_paths(project_root: Path) -> list[str]:
    """The names this project shares between worktrees.

    The project's own setting wins over the default list: a project with a
    different dependency layout names its own. The resolution order is the
    project's order everywhere else (`project_settings.resolve`): the
    environment, the local ignored file, the committed project file, and the
    default.
    """
    import project_settings
    return project_settings.resolve_list(
        project_root, SHARE_SETTING, DEFAULT_SHARED)


def _link_shared(source_root: Path, target_dir: Path, names: list[str]) -> dict[str, Any]:
    """Link the shared caches from `source_root` into `target_dir`.

    Three rules, each learned from what a symlink actually does:

    - **A link is created only when the source exists and the target does
      not.** A link to a place that is not there is a broken link, and
      overwriting a directory that is already there loses data.
    - **A link is created only for a name in the closed list.** Nothing else
      is shared: the list is the whole permission.
    - **The link points at the SOURCE worktree's copy, never at the target's
      own path.** A self-referential link would make the worktree share a
      directory with itself.

    Returns {linked: [...], skipped: [...], failed: [...]}. A failure to link
    one cache never fails the worktree: the worktree is created and usable
    without a shared cache, and the caller is told which ones are missing.
    """
    linked: list[str] = []
    skipped: list[str] = []
    failed: list[str] = []
    for name in names:
        source = source_root / name
        target = target_dir / name
        if not source.exists():
            skipped.append(name)
            continue
        if target.exists() or target.is_symlink():
            skipped.append(name)
            continue
        try:
            target.symlink_to(source.resolve(), target_is_directory=True)
        except OSError:
            failed.append(name)
            continue
        linked.append(name)
    return {"linked": linked, "skipped": skipped, "failed": failed}


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


def _main_worktree(repo_dir: Path) -> Path | None:
    """The main worktree of the repository — the one that owns the branch.

    `git worktree list` names the main worktree first, but "first" is a
    property of the listing, not of the worktree. The unambiguous marker is
    the `.git` entry itself: in the main worktree it is a **directory**, in a
    linked worktree it is a **file** pointing at the common dir. Measured on
    a real pair of worktrees:

        main:    drwxr-xr-x  .git
        linked:  -rw-r--r--  .git

    The main worktree is the one that holds the project's checked-out
    caches: the dependencies a worktree shares are the ones installed in the
    checkout the human works in. A linked worktree is NOT the source — a
    worktree sharing its own dependency directory with itself would defeat
    the point of the isolation.
    """
    for wt in list_worktrees(repo_dir):
        path = Path(wt.get("worktree", ""))
        if not path.exists():
            continue
        if (path / ".git").is_dir():
            return path.resolve()
    return None


def default_branch(repo_dir: Path) -> str:
    """The branch a launch worktree gets when nobody named one.

    The name is DERIVED, not asked for: `chore/pair-<project>-<hash8>`, where
    the hash is over the resolved project path. It has to be stable across
    launches (the same project must land in the same worktree, or every
    launch would create another one), and it has to be unique across
    projects (two projects in one checkout would otherwise fight over one
    branch). The hash gives both: the same path always yields the same
    name, and two paths never do.

    The `chore/` prefix is not decoration. The §20 branch-class table reads
    the class from the NAME, and a name outside the known patterns is an
    ANOMALY — a branch no rule can protect and the janitor may never delete.
    A launch worktree's branch is ephemeral by nature, and it must land in
    the ephemeral class so it can be cleaned up once it is merged.
    """
    root = Path(repo_dir).resolve()
    digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:8]
    name = root.name or "project"
    return f"chore/pair-{name}-{digest}"


def journal_state_dir(repo_dir: Path) -> Path:
    """The state dir the journal is written to: the MAIN repository's `.pair`.

    Not the worktree's own `.pair`: a linked worktree's directory is removed
    with `git worktree remove --force`, and a journal that died with the
    worktree would record nothing about the worktree that died. The main
    worktree is the one that owns the project, so its `.pair` is where the
    lifecycle of every worktree belongs.
    """
    repo_dir = Path(repo_dir)
    main = _main_worktree(repo_dir)
    return (main if main is not None else repo_dir.resolve()) / ".pair"


def branch_commits(repo_dir: Path, branch: str, base: str) -> list[str]:
    """The commits `branch` holds above `base`, oldest first.

    Read at removal time, because that is the last moment the branch's own
    history is still reachable: the merge deletes the branch and the squash
    merge replaces its commits with one new sha on the trunk. A trail that
    did not capture the shas before that moment could only say "something was
    merged", which is exactly what an escape looks like.
    """
    code, out, _ = run_git(["log", "--format=%H", f"{base}..{branch}"], repo_dir)
    if code != 0 or not out:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


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
            # An existing worktree may still miss its shared caches — it was
            # created before the sharing existed, or the caches were removed.
            # Linking what is missing costs nothing and touches nothing that
            # is already there.
            shared = _share_caches(repo_dir, target_dir)
            return {
                "ok": True,
                "created": False,
                "worktree": str(target_dir),
                "branch": wt.get("branch", branch),
                "message": "worktree already exists",
                "shared": shared,
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

    shared = _share_caches(repo_dir, target_dir)
    _journal_created(repo_dir, branch, target_dir)
    return {
        "ok": True,
        "created": True,
        "worktree": str(target_dir),
        "branch": branch,
        "message": "worktree created successfully",
        "shared": shared,
    }


def _journal_created(repo_dir: Path, branch: str, target_dir: Path) -> None:
    """Write the `created` event, and never fail the worktree over it.

    The worktree exists; that is the fact the caller came for. A journal
    that could not be written is a fact the CALLER must know about, but it
    does not un-create the directory — and the writer that ignores the
    refusal would lose the record silently. So the failure is reported in the
    result rather than raised, and the merge gate reads the journal, not the
    return value.
    """
    try:
        worktree_journal_append(
            journal_state_dir(repo_dir), "created",
            branch=branch, path=str(target_dir),
            head=run_git(["rev-parse", "HEAD"], target_dir)[1],
        )
    except (OSError, ValueError):
        return


def _share_caches(repo_dir: Path, target_dir: Path) -> dict[str, Any]:
    """Link the project's shared caches into a freshly created worktree.

    A failure here never fails the worktree: the worktree is created and
    perfectly usable without a shared cache — the cache is a convenience, not
    a dependency of the git work. The caller is told what was linked and what
    was not, so a missing cache is a fact and not a surprise.
    """
    source_root = _main_worktree(repo_dir)
    if source_root is None or source_root == target_dir.resolve():
        return {"linked": [], "skipped": [], "failed": [], "source": None}
    names = shared_paths(repo_dir)
    res = _link_shared(source_root, target_dir, names)
    res["source"] = str(source_root)
    return res


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


def journal_merge(repo_dir: Path, branch: str, base: str, pr_id: str = "",
                  path: str = "", commits: list[str] | None = None) -> dict[str, Any]:
    """Record the merge of a worktree's branch, and the commits it carried.

    The commits are captured BEFORE the merge, by the caller: the merge
    deletes the branch (`--delete-branch`), and after that the shas are
    reachable only through the squash commit on the trunk, which is a
    DIFFERENT sha. A trail that recorded only the squash would lose the
    very thing the trail exists for — which commits the branch held.

    `commits` given by the caller wins; when it is absent the commits are
    read here, which is correct only while the branch still exists.

    Writes one `committed` event per sha and one `merged` event naming
    them. Returns what was written; a failure to write raises, because the
    caller is the merge gate, where a lost record is an escape.
    """
    state_dir = journal_state_dir(repo_dir)
    shas = list(commits) if commits is not None else branch_commits(repo_dir, branch, base)
    for sha in shas:
        worktree_journal_append(state_dir, "committed", branch=branch,
                                 path=path or None, sha=sha)
    merged = worktree_journal_append(state_dir, "merged", branch=branch,
                                     path=path or None, pr=pr_id or None,
                                     commits=shas or None)
    return {"merged": merged, "commits": shas, "state_dir": str(state_dir)}


def journal_removal(repo_dir: Path, branch: str, path: str,
                    commits: list[str] | None = None) -> dict[str, Any]:
    """Record that the worktree's directory was removed.

    The removal is the last event of the lifecycle: after it, the branch is
    gone, the directory is gone, and the journal is the only place that says
    the worktree ever existed.
    """
    return worktree_journal_append(
        journal_state_dir(repo_dir), "removed", branch=branch, path=path,
        commits=commits or None)


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
    # The removal is journaled AFTER the directory is gone: the event says
    # what actually happened, not what was attempted. A journal write that
    # fails here is reported in the result — the directory is already gone
    # and raising would not bring it back.
    journal_error = ""
    try:
        journal_removal(repo_dir, wanted, str(target))
    except (OSError, ValueError) as exc:
        journal_error = str(exc)
    return {"ok": True, "removed": True, "path": str(target), "pruned": pruned,
            "why": "", "journal_error": journal_error}


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
