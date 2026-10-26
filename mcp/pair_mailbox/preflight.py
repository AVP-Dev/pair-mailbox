"""Pre-flight merge check: will this branch merge into the trunk?

Why this exists. Round 147 gave the round branch a clean death: it is merged
and deleted. But the conflict was discovered at the moment of the merge —
after the PR was on review, after the critic had written a verdict, after the
human had read the diff. The cost of finding a conflict late is not the
conflict; it is everything that had to be un-done to find it.

So the question is asked earlier, and asked without touching the repository:
`git merge-tree` computes the merge result in memory and writes nothing.

The three answers are kept apart, because the difference between them is the
whole point:

- **clean** — the merge would succeed (exit 0).
- **conflict** — the merge would fail, and git names the files (exit 1).
- **unknown** — git could not answer: the branch does not exist, git is
  missing, or git failed for any other reason.

The exit code alone does NOT tell these apart. `git merge-tree` exits 1 both
for a conflict AND for a revision it cannot resolve — measured on this host:

    conflict:     exit 1, stdout = `<sha> NUL f.txt NUL NUL <messages>`
    bad revision: exit 1, stdout = empty,   stderr = "not something we can merge"

So the distinction is made on the OUTPUT: git prints the sha of the merge
result only when it actually computed a merge. See `_has_merge_tree`.

`unknown` is NOT `clean`. A gate that answers "no conflicts" when git failed
is worse than no gate at all: it says the thing the reader wants to hear,
without having checked anything. Fail-closed means the failure mode of the
check is a refusal, never a pass.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

#: The exit code git uses for a conflict (`git merge-tree --write-tree`).
CONFLICT_EXIT = 1


def _run_git(args: list[str], cwd: Path) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            ["git"] + args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=60,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except (OSError, subprocess.SubprocessError) as exc:
        return 2, "", str(exc)


def conflicted_files(output: str) -> list[str]:
    """The files git named as conflicting.

    `git merge-tree --write-tree --name-only -z` prints the conflicted paths
    in a machine-readable shape, and the shape is the only thing worth
    parsing: the prose around it is LOCALIZED, and this project runs in a
    Russian locale where git writes «Конфликт слияния в f.txt» instead of
    "Merge conflict in f.txt". A parser that looked for the English phrase
    would find nothing and report a conflict with no files.

    The NUL-separated layout, measured on a real conflict:

        <tree sha> NUL <path> NUL <path> NUL NUL <messages...>

    The names are the tokens between the sha and the first EMPTY token; the
    messages after it are git's prose and are ignored. A clean merge prints
    the sha and an empty token, and nothing else.

    A path with a space or a non-ASCII name survives this unchanged, because
    NUL is the only separator involved.
    """
    tokens = output.split("\0")
    if not tokens or not tokens[0]:
        return []
    names: list[str] = []
    for token in tokens[1:]:
        if not token:
            break
        names.append(token)
    return names


def _has_merge_tree(output: str) -> bool:
    """Did git actually compute a merge?

    `git merge-tree` prints the sha of the merge result as its first token —
    and it prints it ONLY when it computed one. Measured:

    - a conflict:    stdout = `<sha> NUL f.txt NUL NUL <messages>`
    - a clean merge: stdout = `<sha> NUL`
    - a bad revision: stdout = empty, stderr = "not something we can merge"

    So the first token is the difference between "git merged and found a
    conflict" and "git could not merge at all". Both exit 1, which is why the
    exit code alone cannot tell them apart.
    """
    tokens = output.split("\0")
    return bool(tokens and tokens[0].strip())


def check_merge(repo_dir: Path, branch: str, base: str) -> dict[str, Any]:
    """Would `branch` merge into `base` without a conflict?

    Both arguments are REVISIONS, not names to resolve here: the caller
    decides what "the branch" and "the base" are (the local HEAD and
    `origin/<trunk>`, say), because a check that resolves its own revisions
    would compare the wrong pair silently.

    Returns {status, files, why} where `status` is one of:

    - `"clean"` — the merge would succeed;
    - `"conflict"` — it would not, and `files` names the paths;
    - `"unknown"` — git could not answer, and `why` says why.

    Nothing is written: `git merge-tree --write-tree` computes the result and
    prints it, leaving the index, the working tree and the history alone.
    """
    code, out, err = _run_git(
        ["merge-tree", "--write-tree", "--name-only", "-z", branch, base],
        Path(repo_dir))
    if code == 0:
        return {"status": "clean", "files": [], "why": ""}
    if code == CONFLICT_EXIT and _has_merge_tree(out):
        files = conflicted_files(out)
        return {"status": "conflict", "files": files,
                "why": f"{branch} conflicts with {base}"
                       + (f" in {', '.join(files)}" if files else "")}
    detail = (err or out).strip() or f"git merge-tree exited with code {code}"
    return {"status": "unknown", "files": [], "why": detail}


def ensure_commit(repo_dir: Path, remote: str, sha: str) -> dict[str, Any]:
    """Make sure the commit `sha` is available locally.

    The PR's head is a commit SHA that GitHub knows about. This repository
    may not have it: the PR was pushed from another machine, or another
    worktree, or the local clone predates the push. A `merge-tree` against a
    commit git does not have answers "unknown", and an unknown is a refusal
    — so the merge would be blocked by a missing object, not by a conflict.

    Fetching the SHA by name is what fixes it. `git fetch origin <sha>`
    works even when the SHA is not the tip of any branch — measured on a
    single-branch clone that had never seen the feature branch.

    Returns {ok, why}.
    """
    if not sha:
        return {"ok": False, "why": "no commit was given"}
    code, out, err = _run_git(["cat-file", "-e", f"{sha}^{{commit}}"], Path(repo_dir))
    if code == 0:
        return {"ok": True, "why": ""}
    code, out, err = _run_git(["fetch", remote, sha], Path(repo_dir))
    if code != 0:
        detail = (err or out).strip() or f"git fetch {remote} {sha} exited with code {code}"
        return {"ok": False, "why": detail}
    code, out, err = _run_git(["cat-file", "-e", f"{sha}^{{commit}}"], Path(repo_dir))
    if code != 0:
        return {"ok": False, "why": f"the commit {sha} is still not available locally"}
    return {"ok": True, "why": ""}


def fetch_base(repo_dir: Path, remote: str, branch: str) -> dict[str, Any]:
    """Bring the base branch up to date, so the check compares against the
    trunk the merge will actually meet.

    A pre-flight check against a stale base is a check against a fiction: the
    trunk moves while the round works, and the conflict appears exactly when
    the stale base is wrong.

    Returns {ok, why}. A failed fetch is not fatal by itself — the caller
    decides — but the reason is always named, so "the check ran against an
    old trunk" is never silent.
    """
    code, out, err = _run_git(["fetch", remote, branch], Path(repo_dir))
    if code != 0:
        detail = (err or out).strip() or f"git fetch {remote} {branch} exited with code {code}"
        return {"ok": False, "why": detail}
    return {"ok": True, "why": ""}
