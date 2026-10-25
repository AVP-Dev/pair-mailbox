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
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from trunk import resolve_trunk  # noqa: E402 - needs sys.path above

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


def remote_tracking_refs(root: Path) -> list[str] | None:
    """Local remote-tracking refs (`refs/remotes/*`), or None when unknown.

    Local only, never the network: `ls-remote`/`fetch`/`gh` have no place in the
    `pair_state` hot path (`server.py:1789` runs on every state read, and auth
    prompts plus offline machines would hang the pair's most frequent call).
    When the writer pushes, git updates `refs/remotes/origin/<branch>` as part of
    the push, so the writer's own delivery is visible here immediately.

    Honest caveat, not hidden: these refs are a cache. A push made by someone
    else, or on another machine, stays invisible until a `fetch`. `None` means
    "cannot check" (no git at all, or no remote configured, or zero refs after
    a never-fetched remote) — never "nothing was pushed". Collapsing those two
    is exactly the failure A5 guards against.
    """
    remotes = _run_git(root, "remote")
    if remotes is None:
        return None
    if not remotes.strip():
        return None
    out = _run_git(root, "for-each-ref", "--format=%(refname)", "refs/remotes/")
    if out is None:
        return None
    return sorted({ln.strip() for ln in out.splitlines() if ln.strip()})


def head_reachable_from(root: Path, ref: str, head: str = "") -> bool | None:
    """Whether `head` (default HEAD) is an ancestor of `ref`, asked locally.

    Three-valued: True/False on a clean answer, None when git itself broke
    (timeout, lock, damage). `_run_git` quenches exit-1 and exceptions into one
    None, so this re-runs the probe raw: exit 0 means ancestor, exit 1 means
    not-ancestor, anything else (timeout, signal, no git) means cannot-check.
    Collapsing broken-git into False would score a cannot-check as
    \"undelivered\" — the mirror of the failure this round exists to close.
    """
    import subprocess

    head = head or git_head(root)
    if not head or not ref:
        return False
    try:
        proc = subprocess.run(
            ["git", "merge-base", "--is-ancestor", head, ref], cwd=str(root),
            capture_output=True, text=True, timeout=5,
        )
    except Exception:  # noqa: BLE001 - broken git is unknown, never undelivered
        return None
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    return None


def delivery_floor(root: Path) -> tuple[str, str]:
    """The reachability floor: is HEAD visible from any remote-tracking ref?

    Returns `(status, detail)` with status one of:
    - `"delivered"`: HEAD is an ancestor of some `refs/remotes/*` (A1, A2);
    - `"undelivered"`: git works but no remote ref reaches HEAD (A3) —
      detail names the push command to run;
    - `"unknown"`: cannot check — no git, no remote, a never-fetched remote
      with zero refs (A4, A5, A6), or a broken probe mid-way (timeout, lock,
      damage after the earlier git calls succeeded). Loud, never a pass,
      never a hard block.
    """
    head = git_head(root)
    if not head:
        return "unknown", "not a git repository: delivery cannot be checked"
    refs = remote_tracking_refs(root)
    if refs is None:
        return "unknown", "no remote configured: delivery cannot be checked"
    if not refs:
        return "unknown", "remote configured but no refs/remotes/* yet (never fetched): delivery cannot be checked"
    broken = False
    for ref in refs:
        answer = head_reachable_from(root, ref, head)
        if answer is True:
            return "delivered", ref
        if answer is None:
            broken = True
    if broken:
        return "unknown", "reachability probe failed mid-check (timeout, lock, or damage): delivery cannot be checked"
    return "undelivered", "HEAD is reachable from no refs/remotes/*: push first (`git push -u origin <branch>`)"

def _repo_slug(root: Path) -> str:
    """`owner/repo` from the origin URL, or empty when unknowable.

    Parses `git remote get-url origin` for the GitHub path, accepting SSH
    (`git@github.com:owner/repo.git`) and HTTPS forms. Empty on any failure:
    the ceiling then skips the merged lookup and falls through honestly,
    never blocking on a slug it could not read.
    """
    import re

    out = _run_git(root, "remote", "get-url", "origin")
    if not out:
        return ""
    url = out.strip()
    match = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$", url)
    return match.group(1) if match else ""


def pr_ceiling(root: Path, autonomy: str) -> tuple[str, str]:
    """The PR ceiling: does a verified PR state match the autonomy level?

    Advance-path only, never the `pair_state` hot path: `gh` is network, and a
    network call inside every state read repeats Trap 1 the floor was built to
    avoid. Callers in the state path carry an advisory line instead (B7).

    Never trusts a `kind=pr` evidence row: the value is a string a role wrote,
    and \"a record indistinguishable from a run is exactly what the gate must
    never close on\" (`server.py:451-460`). Verification runs against `gh`,
    and E1/E3 follow from that: a row naming a missing PR blocks because `gh`
    never heard of it.

    Returns `(status, detail)`:
    - `\"resolved\"`: semi+open (B1), semi+merged (B2, merged is stronger, not a
      failure), auto+merged (B4);
    - `\"missing\"`: semi+no-PR (B3), auto+open-unmerged (B5), evidence row for a
      PR that does not exist (E3) — blocks, detail names what to do;
    - `\"unknown\"`: `gh` absent or unauthenticated (B8) — loud, no hard block,
      never scored as a pass.
    `mode=audit` never calls here (B6, D3): the caller exempts audit first.
    """
    import json
    import subprocess

    def run_gh(*args: str) -> tuple[int, str]:
        try:
            proc = subprocess.run(
                ["gh", *args], cwd=str(root),
                capture_output=True, text=True, timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            return 1, ""
        return proc.returncode, (proc.stdout or "").strip()

    head = git_head(root)
    if not head:
        return "unknown", "not a git repository: PR state cannot be checked"
    code, out = run_gh("pr", "list", "--state", "open",
                        "--json", "number,headRefOid,headRefName")
    if code != 0:
        return "unknown", "gh unavailable or unauthenticated: PR state cannot be checked, not scored as a pass"
    try:
        open_prs = json.loads(out or "[]")
    except ValueError:
        return "unknown", "gh returned unreadable PR state: cannot be checked, not scored as a pass"
    if not isinstance(open_prs, list):
        return "unknown", "gh returned unreadable PR state: cannot be checked, not scored as a pass"
    open_for_head = [pr for pr in open_prs
                     if isinstance(pr, dict) and pr.get("headRefOid") == head]
    # Merged lookup, no page ceiling: `gh api .../commits/<oid>/pulls` asks
    # which PRs the commit itself is associated with, instead of paging closed
    # history (Finding 2: --limit 50 goes silently stale past 50 closed PRs).
    # Verified live: the merge commit 506bcf4 returns PR 36, the pre-merge base
    # 9387d74 returns []. One targeted call, no pagination, no branch-name
    # guessing (merge-branch parents are local oids, not ref names).
    merged = False
    slug = _repo_slug(root)
    if slug and head:
        code, assoc_out = run_gh("api", f"repos/{slug}/commits/{head}/pulls")
        if code == 0:
            try:
                assoc = json.loads(assoc_out or "[]")
            except ValueError:
                assoc = []
            if isinstance(assoc, list):
                merged = any(
                    isinstance(pr, dict) and pr.get("merged_at") is not None
                    for pr in assoc
                )
    if open_for_head:
        if autonomy == "auto":
            numbers = ", ".join(f"#{pr['number']}" for pr in open_for_head)
            return ("missing",
                    f"PR {numbers} open but unmerged: autonomy is auto, "
                    f"which means end to end — merge it, or set semi to stop at prepared")
        names = ", ".join(f"#{pr['number']}" for pr in open_for_head)
        return "resolved", f"PR {names} open at semi"
    if merged:
        return "resolved", f"HEAD merged to {resolve_trunk(root)}"
    if autonomy == "semi":
        return ("missing",
                "no open PR for HEAD: prepare one (`gh pr create`), "
                "the human word (`bin/pairctl ok`) is still owed")
    return ("missing",
            "HEAD merged by no PR and carried by no open PR: "
            "merge first — autonomy is auto, which means end to end")

def unanchored_trunk_commits(root: Path, base: str) -> list[str]:
    """Commits on the trunk after `base` — the close-side leg of the lane.

    The ceiling associates only HEAD (`commits/<oid>/pulls`), so a post-merge
    commit on the trunk is invisible by design: the PR carried the merge, not
    the hotfix that followed it. That blind spot is why round 104's seven
    direct commits read as no-PR at close.

    The trunk is the project's own branch (`trunk.resolve_trunk`), not a
    hardcoded `master`: a project whose trunk is `dev` must be measured
    against `dev`, or its rounds can never close.
    """
    if not base:
        return []
    trunk_ref = f"origin/{resolve_trunk(root)}"
    out = _run_git(root, "log", "--format=%H", f"{base}..{trunk_ref}")
    return [line.strip() for line in out.splitlines() if line.strip()] if out else []


def hotfix_trailer_check(root: Path, base: str) -> tuple[bool, str]:
    """Every commit after `base` on the trunk must name its parent PR.

    Trailer `follows: PR #N`, verified merged through `gh`. An unknown or
    unmerged N REFUSES — the leg never assigns itself a number, because the
    defect it exists to catch is work reaching the trunk without review.

    Returns `(ok, detail)`; `ok=True` only when every commit names a verified
    merged PR, or when nothing sits after base (nothing to name).
    """
    import json
    import re

    # F1 (critic finding-slice3): an empty commit list is ambiguous between
    # "nothing after base" (a pass) and "git could not answer" (a refusal).
    # The rule this slice exists for is loud refusal, never a pass — so the
    # two inputs are resolved BEFORE the list is read, and a failure refuses
    # instead of silently reading as an empty history.
    if not base or not _run_git(root, "rev-parse", "--verify", f"{base}^{{commit}}"):
        return False, (f"round base '{base or 'unset'}' does not resolve: "
                       f"cannot locate the commits to check")
    trunk_ref = f"origin/{resolve_trunk(root)}"
    head_ref = _run_git(root, "rev-parse", "--verify", trunk_ref)
    if not head_ref:
        return False, f"{trunk_ref} does not resolve: cannot locate the commits to check"

    commits = unanchored_trunk_commits(root, base)
    if not commits:
        return True, f"no commits after the round base on {trunk_ref}"

    trailer_re = re.compile(r"^follows:\s*PR\s*#(\d+)\s*$", re.IGNORECASE | re.MULTILINE)
    slug = _repo_slug(root)
    unnamed: list[str] = []
    unverified: list[str] = []
    for sha in commits:
        body = _run_git(root, "log", "-1", "--format=%B", sha) or ""
        m = trailer_re.search(body)
        if not m:
            unnamed.append(sha[:8])
            continue
        number = m.group(1)
        if not slug:
            unverified.append(
                f"{sha[:8]} names PR #{number} but the repo slug is unreadable: cannot verify it merged")
            continue
        code, out = _run_gh(root, "pr", "view", number, "--json", "number,state,mergedAt")
        if code != 0:
            unverified.append(f"{sha[:8]} names PR #{number}, which gh does not know")
            continue
        try:
            pr = json.loads(out or "{}")
        except ValueError:
            pr = {}
        if not pr.get("mergedAt"):
            unverified.append(f"{sha[:8]} names PR #{number} which is not merged")
    if unnamed or unverified:
        parts = []
        if unnamed:
            parts.append("commit(s) on the trunk naming no parent PR: " + ", ".join(unnamed))
        parts.extend(unverified)
        return False, "; ".join(parts)
    return True, f"{len(commits)} commit(s) after base, each naming a verified merged PR"


def _run_gh(root: Path, *args: str) -> tuple[int, str]:
    """One `gh` call, or (1, "") when the binary or the network is absent."""
    import subprocess
    try:
        proc = subprocess.run(["gh", *args], cwd=str(root),
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    return proc.returncode, (proc.stdout or "").strip()


def origin_truth(root: Path, round_base: str) -> tuple[str, str]:
    """Round 105 slice 2b: does the LOCAL head match what origin carries?

    The floor answers for local head only — "HEAD is reachable from some
    remote-tracking ref" — so a reset backwards read `delivered` while origin sat
    7 ahead (round 104's measured false pass). This comparison asks the other
    question: how far is LOCAL from where the round started and what origin has.

    Returns `(status, detail)`:
    - `"aligned"`: local head is an ancestor-or-equal of the trunk ref —
      nothing of ours is missing from origin, and nothing on origin is
      missing locally.
    - `"behind"`: the trunk carries commits local does not; the count is
      named, because "behind" with no number is not actionable.
    - `"ahead"`: local head carries commits origin does not (push first).
    - `"unknown"`: no round_base recorded (a fresh round: nothing to diff
      against), no git, no trunk ref, or a probe that failed mid-way —
      loud, never scored as a pass.
    """
    if not round_base:
        return "unknown", "no round_base recorded: nothing to diff against (pin it at round open)"
    head = git_head(root)
    if not head:
        return "unknown", "not a git repository: origin truth cannot be checked"
    if not _run_git(root, "rev-parse", "--verify", f"{round_base}^{{commit}}"):
        return "unknown", f"round base {round_base[:12]} does not resolve: cannot compare"
    trunk_ref = f"origin/{resolve_trunk(root)}"
    if not _run_git(root, "rev-parse", "--verify", trunk_ref):
        return "unknown", f"{trunk_ref} does not resolve: cannot compare"

    behind = _run_git(root, "rev-list", "--count", f"HEAD..{trunk_ref}")
    ahead = _run_git(root, "rev-list", "--count", f"{trunk_ref}..HEAD")
    if behind is None or ahead is None:
        return "unknown", "rev-list failed mid-check (timeout, lock, or damage): origin truth cannot be checked"
    behind = behind.strip()
    ahead = ahead.strip()
    if not behind.isdigit() or not ahead.isdigit():
        return "unknown", "rev-list answered unreadable counts: origin truth cannot be checked"
    n_behind, n_ahead = int(behind), int(ahead)
    if n_behind:
        return "behind", f"{n_behind} commits behind {trunk_ref}: pull/rebase before the round can ship"
    if n_ahead:
        return "ahead", f"{n_ahead} commits ahead of {trunk_ref}: push first (`git push -u origin <branch>`)"
    return "aligned", f"local head equals {trunk_ref}"


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


def resolve_head(root: Path, head: str) -> str:
    "Full SHA for an explicit revision, or '' when git cannot resolve it."
    if not (head or "").strip():
        return ""
    out = _run_git(root, "rev-parse", "--verify", (head or "").strip())
    return (out or "").strip()


def pin_verdict(store: Any, role: str, kind: str, text: str, utcnow: Any,
                head: str | None = None) -> dict[str, Any]:
    """Bind the verdict to the revision: HEAD + dirty-hash + files.

    Moved out of server.tool_pair_note (server-py-still-a-monolith): the binding
    lives beside the snapshot building it. Silently applying a verdict to a moved
    revision reads worse than no verdict. Fail-open: with no git an empty snapshot
    writes, never dropping the verdict.

    An explicit `head` anchors the record to that revision instead of the live
    tree (round 97: the re-pin after a branch rebuild landed on the mid-rebuild
    HEAD). The anchor is only an anchor — staleness still reads CONTENT
    (changed_between + overlap), so pinning an unreviewed tree stays stale.
    An unresolvable head raises ValueError: an explicit request must fail
    loudly, never silently bind elsewhere.
    """
    root = store.project_root
    if head:
        resolved = resolve_head(root, head)
        if not resolved:
            raise ValueError(f"cannot resolve head {head!r}")
        live_dirty = dirty_files(root, include_untracked=False)
        h_files = head_files(root, resolved)
        files = sorted(set(h_files) | set(live_dirty)) if h_files else live_dirty
        rev = {"head": resolved, "dirty": dirty_hash(root), "files": files}
    else:
        rev = snapshot(root)

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
