"""PR merge machine gate and Feature Freeze (round 60, task #67).

The module implements strict machine protection against PR merges before review ends:
1. `check_pr_merge(store: Store, repo_dir: Path) -> tuple[bool, str]`:
   - Refuse when the current phase is not `verify` (merging banned in scoping, plan, implement, done).
   - Refuse with no git repo (fail-closed on the merge path).
   - Refuse with no successful test evidence in the round (`kind="test"`, `ok=True`).
   - Refuse with no critic verdict or a non-approved last verdict (reject / no approve).
   - Self-approval ban: refuse when the verdict comes from the writer (`role == store.writer`).
   - Freshness check: direct reuse of `review.is_stale(...)`. Any new commit or
     uncommitted file change turns the verdict stale and blocks the merge.
2. `cmd_pr(store: Store, args: argparse.Namespace) -> int`:
   - The `pairctl pr merge [pr_id] [--force] [--reason REASON]` CLI command.
   - The `--force` flag demands a non-empty `--reason`, recorded in the transcript as `pr_merge_forced`.
   - On a passed check calls `gh pr merge <pr_id> --squash --delete-branch`.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Any

import review
import merge_strategy
from trunk import resolve_trunk


def check_pr_merge(store: Any, repo_dir: Path, pr_head: str | None = None) -> tuple[bool, str]:
    """Check whether the PR merge passes the protocol rules.

    `pr_head` is the commit the PR would actually merge, as GitHub reports it
    (`gh pr view --json headRefOid`). The gate reads the local HEAD, so a PR
    whose head is not the local HEAD would ship a commit nobody checked here.

    Returns (True, "") on a passed check or (False, refusal_reason).
    """
    head = review.git_head(repo_dir)
    if not head:
        return False, "cannot verify: not a git repo"

    state = store.read()
    phase = state.get("phase")
    if phase != "verify":
        return False, f"phase is {phase!r}, PR merge allowed strictly in 'verify'"

    pr = str(pr_head or "").strip()
    if pr and pr.lower() != head.lower():
        return False, (
            f"the PR head {pr[:12]} is not the local HEAD {head[:12]}: "
            f"the merge would ship a commit this gate did not check"
        )

    # Check for successful test evidence in the current round
    cur_round = int(state.get("round", 0))
    round_ev = [
        ev for ev in (state.get("evidence") or [])
        if int(ev.get("round", -1)) == cur_round
    ]
    test_ev = [
        ev for ev in round_ev
        if ev.get("kind") == "test" and ev.get("ok")
    ]
    if not test_ev:
        return False, f"round {cur_round} has no passing test evidence (kind='test', ok=True)"

    # The evidence must name the commit it was measured on, and that commit must
    # be the one being merged. A record bound to the round alone keeps vouching for
    # every commit after it: the round number does not move when the head does
    # (the card: merge-gate-does-not-require-ci-for-the-head).
    head_lower = head.lower()
    if not any(str(ev.get("head") or "").strip().lower() == head_lower for ev in test_ev):
        named = sorted({
            str(ev.get("head") or "").strip()[:12]
            for ev in test_ev if str(ev.get("head") or "").strip()
        })
        return False, (
            f"no passing test evidence names the head being merged ({head[:12]}); "
            f"the evidence names {', '.join(named) or 'no commit'}: "
            f"run the suite on this commit and record the evidence again"
        )

    # Check the critic verdict
    verdicts = state.get("verdicts") or []
    if not verdicts:
        return False, "no verdict found: peer review approval is required before merge"

    last_verdict = verdicts[-1]
    author = last_verdict.get("role")
    writer = state.get("writer")

    # Self-approval ban: the writer cannot approve its own PR
    if writer and author == writer:
        return False, f"self-approval forbidden: verdict author {author!r} is current writer"

    # The house style pins every verdict-* slug with the slug as the kind
    # (server.py:3423: kind == "verdict" or kind.startswith("verdict-")),
    # so the gate accepts the family, not one literal — or fifteen merges
    # bypass it with nobody noticing (round 91: 0 pr_merge events, and the
    # only verdicts the project produces are verdict-roundNN-critic).
    v_rev = last_verdict.get("rev") or {}
    v_kind = last_verdict.get("kind")
    if v_kind != "verdict" and not str(v_kind or "").startswith("verdict-"):
        return False, f"last review note is not a verdict (kind={v_kind!r})"

    # The verdict must bind to a revision (head)
    v_head = str(v_rev.get("head") or "").strip()
    if not v_head:
        return False, "verdict has no revision binding (rev.head is empty)"

    # Check the last verdict text from its own note file (fail-closed: the
    # file must exist). The verdict record carries its kind as the filename:
    # notes/verdict-<slug>.md, falling back to notes/verdict.md for the old
    # single-file style. Reading only verdict.md would judge every verdict-*
    # note against round 86's text (round 91: the live refusal quoted
    # verdict.md's tail while the verdict was verdict-round91-refresh).
    notes_dir = store.notes_root
    verdict_file = notes_dir / f"{v_kind}.md" if v_kind != "verdict" else notes_dir / "verdict.md"
    if not verdict_file.is_file():
        verdict_file = notes_dir / "verdict.md"
    if not verdict_file.is_file():
        return False, f"notes/{verdict_file.name} does not exist: approval text cannot be verified"

    import re
    try:
        content = verdict_file.read_text(encoding="utf-8")
        blocks = [b for b in content.split("\n## ") if b.strip()]
        if not blocks:
            return False, f"notes/{verdict_file.name} is empty: no verdict text found"
        # D1: Machine approve channel — the structured 'verdict-approve
        # <head|round>' marker, or the house verdict heading (**APPROVED** /
        # **NEEDS WORK** — round 91 proved the gate must read what the project
        # actually writes, not one fixture's marker). The heading decides:
        # APPROVED passes here, NEEDS WORK refuses via the reject branch
        # below. No prose false fires: bare "approved"/"looks good" never
        # counts — only the bold verdict heading or the structured line.
        VERDICT_APPROVE_RE = re.compile(
            r"^\s*verdict-approve\s+(?:round\s+(\d+)|([0-9a-f]{7,40}))\b"
            r"|^\*\*(APPROVED)(\*\*)?",
            re.IGNORECASE | re.MULTILINE,
        )
        # The refuse branch tolerates prose (a false refusal is safe)
        VERDICT_REJECT_RE = re.compile(
            r"(?:\bverdict-reject\b|^\*\*(NEEDS WORK)(\*\*)?|ВЕРДИКТ\b[^\n]*\b(?:ОТКАЗ|REJECT|CHANGES REQUESTED)\b|\bне\s+одобрен[оаы]?\b)",
            re.IGNORECASE | re.MULTILINE,
        )
        # Round 97 (C1): the newest MARKED block decides — a marker in an old
        # block never leaks into the decision. Pin reconciliation (D2) applies
        # to the deciding block only, failing into refusal, never falling back
        # to an older marker that happens to match HEAD. Reject wins ties
        # within one block (checked first, as before).
        deciding = None
        for b in reversed(blocks):
            marked = bool(VERDICT_REJECT_RE.search(b) or VERDICT_APPROVE_RE.search(b))
            if marked:
                deciding = b
                break
        if deciding is None:
            # C2(i): no marker in any block — named cause, not silence.
            return False, (f"last verdict is not approved (no structured 'verdict-approve <head|round>' "
                           f"marker or verdict heading in any of {len(blocks)} section(s))")
        if VERDICT_REJECT_RE.search(deciding):
            # C2(iii): reject overrules approve inside the deciding block.
            return False, "last verdict rejected the changes (REFUSE / REJECT / NOT APPROVED in the deciding section)"

        approve_match = VERDICT_APPROVE_RE.search(deciding)
        if not approve_match:
            # Unreachable by construction (deciding is marked and not
            # rejected), kept fail-closed so a future regex edit cannot
            # silently approve.
            return False, "last verdict is not approved (deciding section carries no approval marker)"

        # D2: Reconcile the marker value with the verdict (v_head) or the current HEAD / current round.
        # A bare **APPROVED** carries no head/round: then the pin is the
        # verdict record itself — rev.head must equal HEAD and be fresh
        # (is_stale below), or the marker means nothing.
        groups = approve_match.groups()
        m_round, m_head = (groups[0], groups[1]) if len(groups) >= 2 else (None, None)
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
        else:
            # Bare **APPROVED**: the verdict pin must be HEAD itself.
            if v_head.lower() != head.lower():
                return False, f"verdict pins {v_head[:12]} but HEAD is {head[:12]}: re-review before merge"
    except OSError as err:
        return False, f"failed to read notes/verdict.md: {err}"

    # U3: Merge-path guard — refuse on any dirty tree (tracked or untracked).
    # Merging allowed strictly on a clean committed tree ("merge what is checked and committed").
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

    # Freshness check through the single canonical review.is_stale predicate
    cur_dirty = review.dirty_files(repo_dir)
    changed = review.changed_between(repo_dir, v_head, head)
    stale, reason = review.is_stale(v_rev, head, changed, cur_dirty)
    if stale:
        return False, f"verdict is stale: {reason}"

    return True, ""


def run_gh_merge(repo_dir: Path, pr_id: str | None) -> tuple[int, str]:
    """Run the merge through the gh cli.

    The strategy comes from the project's setting, not from a hard-wired
    flag: `squash` by default (the behaviour that existed before the
    setting existed), `merge` or `rebase` when the project asks for them.
    `--delete-branch` is always there — §19 requires the round's branch to
    die with the round.

    An invalid strategy is a REFUSAL with the admissible values named, not a
    silent fall back to squash: a project that asked for `rebase` and got
    `squash` would learn it from the trunk's history, where nobody was
    looking.
    """
    try:
        flags = merge_strategy.merge_flags(repo_dir)
    except ValueError as exc:
        return 1, str(exc)
    cmd = ["gh", "pr", "merge"]
    if pr_id:
        cmd.append(pr_id)
    cmd.extend(flags)
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


def resolve_pr_head(repo_dir: Path, pr_id: str | None) -> tuple[str, str]:
    """Ask GitHub which commit the PR would merge.

    The gate looks at the local HEAD; the merge ships the PR's head. When the two
    differ, the merge would ship a commit nobody checked here — so the gate needs
    the PR's head, and no answer means no merge.
    """
    cmd = ["gh", "pr", "view"]
    if pr_id:
        cmd.append(str(pr_id))
    cmd.extend(["--json", "headRefOid", "--jq", ".headRefOid"])
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=30,
        )
    except OSError as err:
        return "", f"failed to execute gh: {err}"
    except subprocess.TimeoutExpired:
        return "", "gh pr view timed out"
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
        return "", detail
    oid = proc.stdout.strip()
    if not oid:
        return "", "gh pr view reported no head oid"
    return oid, ""


def run_gh_view(repo_dir: Path, pr_id: str) -> tuple[int, str]:
    """Ask GitHub for a PR's real merge state (audit-repair path only)."""
    cmd = [
        "gh",
        "pr",
        "view",
        pr_id,
        "--json",
        "state,mergeCommit,headRefOid",
        "--jq",
        r'"\(.state) \(.mergeCommit.oid // "NONE") \(.headRefOid)"',
    ]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=30,
        )
        return proc.returncode, (proc.stdout + "\n" + proc.stderr).strip()
    except OSError as err:
        return 1, f"failed to execute gh: {err}"
    except subprocess.TimeoutExpired:
        return 1, "gh pr view timed out"


def _cmd_pr_merge_record(store: Any, args: argparse.Namespace) -> int:
    """Audit repair: record a merge that bypassed this gate (PROTOCOL §10g).

    Round 115 closed a PR with a hand-run `gh pr merge`, so the merge happened
    and the transcript never learned of it — 456 rows with zero merge events.
    The repair writes the missing event and names the real merge commit GitHub
    reports, refusing when `--head` disagrees: a trail repaired by hand is still
    a claim, and this way the claim is checkable against the remote.
    """
    pr_id = getattr(args, "pr_id", None)
    if not pr_id:
        print("error: pr merge-record needs a PR id", file=sys.stderr)
        return 2
    repo_dir = store.project_root
    base = getattr(args, "base", None) or resolve_trunk(repo_dir)
    code, out = run_gh_view(repo_dir, str(pr_id))
    if code != 0:
        print(f"cannot verify PR {pr_id} on the remote:\n{out}", file=sys.stderr)
        return 1
    parts = out.split()
    if len(parts) < 3:
        print(f"unparseable gh output for PR {pr_id}: {out.strip()}", file=sys.stderr)
        return 1
    state, merge_sha, head_oid = parts[0], parts[1], parts[2]
    if state != "MERGED":
        print(f"refusing: PR {pr_id} is {state}, not MERGED", file=sys.stderr)
        return 1
    claimed = str(getattr(args, "head", "") or "")
    if claimed and claimed != merge_sha:
        print(
            "refusing: head mismatch — you gave "
            f"{claimed[:12]}, GitHub merged {merge_sha[:12]}",
            file=sys.stderr,
        )
        return 1
    store.append_transcript(
        "pr_merge_repaired",
        getattr(args, "role", "") or "writer",
        pr_id=pr_id,
        merge_commit=merge_sha,
        head_oid=head_oid,
        base=base,
    )
    print(
        f"audit repaired: PR {pr_id} merged as {merge_sha[:12]} into {base}; "
        "transcript now knows what a hand-run merge hid"
    )
    return 0


def cmd_pr(store: Any, args: argparse.Namespace) -> int:
    """Handler for the `pairctl pr` subcommands."""
    action = getattr(args, "pr_action", None)
    if action == "merge-record":
        return _cmd_pr_merge_record(store, args)
    if action != "merge":
        print(f"unknown pr action: {action!r}", file=sys.stderr)
        return 2

    repo_dir = store.project_root
    force = bool(getattr(args, "force", False))
    reason = str(getattr(args, "reason", "") or "").strip()
    pr_id = getattr(args, "pr_id", None)

    if force:
        if not reason:
            print("error: --force requires --reason with explanation", file=sys.stderr)
            return 2
        # The critic residual demand: the bypass never serves the writer alone,
        # a prior human approval is required (pairctl ok on the current round)
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
        pr_head, head_err = resolve_pr_head(repo_dir, pr_id)
        if head_err:
            print(
                f"PR merge gate REFUSED: cannot read the PR's head: {head_err}",
                file=sys.stderr,
            )
            return 1
        ok, why_not = check_pr_merge(store, repo_dir, pr_head=pr_head)
        if not ok:
            print(f"PR merge gate REFUSED: {why_not}", file=sys.stderr)
            return 1

    # The pre-flight conflict check runs on BOTH paths, forced or not.
    #
    # `--force` bypasses the REVIEW gates: the critic's verdict, the test
    # evidence, the freshness rule. A conflict is not a review gate — it is
    # a fact about the two commits, and it does not become true because a
    # human wrote a reason next to it. GitHub would refuse the merge
    # anyway; refusing here says WHY, before the PR was torn down and
    # rebuilt.
    pf_ok, pf_why = _preflight_merge(repo_dir, pr_id)
    if not pf_ok:
        print(f"PR merge gate REFUSED: pre-flight conflict check: {pf_why}",
              file=sys.stderr)
        return 1

    # The branch's own commits are captured BEFORE the merge: the merge
    # deletes the branch (`--delete-branch`), and after that the shas are
    # reachable only through the squash commit, which is a different sha.
    # Captured late, the journal would say "something was merged" and name
    # no commit — the same shape as a merge that escaped the gate.
    pre_commits = _pre_merge_commits(repo_dir, pr_id)

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
    _journal_merge(store, repo_dir, pr_id,
                   getattr(args, "role", "") or "writer",
                   pre_commits=pre_commits)
    # The cleanup runs AFTER the merge, and it must never break the command:
    # the merge already happened and cannot be undone, so a housekeeping error
    # that escaped here would hide a real merge behind a failed exit code.
    try:
        _cleanup_worktree_after_merge(store, repo_dir, pr_id,
                                      role=getattr(args, "role", "") or "writer")
    except Exception as exc:  # noqa: BLE001 - the merge is already done
        store.append_transcript(
            "worktree_cleanup_failed",
            getattr(args, "role", "") or "writer",
            pr_id=pr_id,
            error=str(exc),
        )
    return 0


def _pre_merge_commits(repo_dir: Path, pr_id: str | None) -> list[str]:
    """The commits the PR's head branch holds above the trunk, oldest first.

    Read BEFORE the merge, while the branch still exists. A failure to read
    them is an empty list, not an exception: the merge proceeds and the
    journal records the merge without the commit list, which is a weaker
    record but an honest one.
    """
    branch = _merged_branch(repo_dir, pr_id)
    if not branch:
        return []
    try:
        from worktree import branch_commits
    except ImportError:  # pragma: no cover - worktree sits next in the package
        return []
    try:
        return branch_commits(repo_dir, branch, resolve_trunk(repo_dir))
    except Exception:  # noqa: BLE001 - a weaker record, never a failed merge
        return []


def _journal_merge(store: Any, repo_dir: Path, pr_id: str | None,
                   role: str, pre_commits: list[str] | None = None) -> None:
    """Write the merge into the DURABLE worktree journal.

    The transcript rotates: four archives, the oldest deleted. A merge
    record that rides on the transcript leaves the window, and a merge that
    left the window is indistinguishable from a merge that escaped the gate.
    The journal in `.pair/worktree-log.jsonl` is never rotated, so the trail
    of every merged worktree survives the room that made it.

    The commits are captured HERE, before the cleanup removes the worktree
    and the merge deletes the branch: after that the shas are reachable only
    through the squash commit, which is a different sha.

    A failure to write the journal is recorded in the transcript and does
    NOT fail the merge: the merge already happened and cannot be undone.
    But it is recorded, because a silent missing record is exactly the
    escape the journal exists to make visible.
    """
    try:
        from worktree import journal_merge, journal_state_dir
    except ImportError:  # pragma: no cover - worktree sits next in the package
        return
    branch = _merged_branch(repo_dir, pr_id)
    if not branch:
        return
    try:
        base = resolve_trunk(repo_dir)
        res = journal_merge(repo_dir, branch, base, pr_id=str(pr_id or ""),
                            commits=pre_commits)
    except Exception as exc:  # noqa: BLE001 - the merge is already done
        store.append_transcript(
            "worktree_journal_failed",
            role or "writer",
            pr_id=pr_id,
            branch=branch,
            error=str(exc),
        )
        return
    store.append_transcript(
        "worktree_merged",
        role or "writer",
        pr_id=pr_id,
        branch=branch,
        commits=res.get("commits") or [],
        journal=str(Path(res["state_dir"]) / "worktree-log.jsonl"),
    )


def _preflight_merge(repo_dir: Path, pr_id: str | None) -> tuple[bool, str]:
    """Would the PR's head merge into the trunk without a conflict?

    The trunk is brought up to date first: a check against a stale trunk is a
    check against a fiction, and the conflict appears exactly when the stale
    trunk is wrong.

    The comparison is between the PR's **head** and `origin/<trunk>`. The
    PR's head is what the merge would ship; the local HEAD is what the
    gate checked, and when they differ the merge would ship something
    nobody looked at.

    Returns (True, "") when the merge would be clean. Every other answer —
    a conflict, a git failure, a trunk that cannot be read — is a refusal
    with a reason. `unknown` is never read as `clean`: a check that says
    "no conflicts" when git failed says the thing the reader wants to hear
    without having checked anything.
    """
    import preflight

    pr_head, head_err = resolve_pr_head(repo_dir, pr_id)
    if head_err or not pr_head:
        return False, (head_err or "the PR's head could not be read")

    trunk_name = resolve_trunk(repo_dir)
    base = f"origin/{trunk_name}"

    fetched = preflight.fetch_base(repo_dir, "origin", trunk_name)
    if not fetched["ok"]:
        return False, (f"the trunk {base} could not be fetched: {fetched['why']}")

    # The PR's head may not be in this repository at all — the PR was
    # pushed from elsewhere. Without the commit, `merge-tree` can only
    # answer "unknown", and an unknown is a refusal. Fetching it by SHA is
    # what makes the check possible.
    have = preflight.ensure_commit(repo_dir, "origin", pr_head)
    if not have["ok"]:
        return False, (f"the PR's head {pr_head[:12]} could not be fetched: "
                       f"{have['why']}")

    res = preflight.check_merge(repo_dir, pr_head, base)
    if res["status"] == "clean":
        return True, ""
    if res["status"] == "conflict":
        return False, res["why"]
    return False, f"the merge could not be checked: {res['why']}"


def _cleanup_worktree_after_merge(store: Any, repo_dir: Path, pr_id: str | None,
                                  role: str = "writer") -> None:
    """Remove the worktree the merged branch lived in, and say what happened.

    The commits are in the trunk and the branch is gone (`--delete-branch`);
    the DIRECTORY that held it is left over. §19 makes the merged round branch
    die by construction, and this makes the directory die with it.

    Two rules, both learned the hard way:

    - **A failed cleanup never turns a successful merge into a failure.** The
      merge already happened and cannot be undone; refusing to report success
      would hide a real merge behind a housekeeping error.
    - **The cleanup is recorded either way.** Silence would leave the reader
      unable to tell "there was nothing to clean" from "the cleanup failed and
      nobody said so" — two states with different actions.
    """
    try:
        from worktree import cleanup_worktree_for_branch
    except ImportError:  # pragma: no cover - worktree sits next in the package
        return
    branch = _merged_branch(repo_dir, pr_id)
    if not branch:
        store.append_transcript(
            "worktree_cleanup_skipped",
            role or "writer",
            pr_id=pr_id,
            why="the merged branch could not be read",
        )
        return
    res = cleanup_worktree_for_branch(repo_dir, branch)
    store.append_transcript(
        "worktree_cleaned" if res.get("removed") else "worktree_cleanup_skipped",
        role or "writer",
        pr_id=pr_id,
        branch=branch,
        path=res.get("path"),
        removed=bool(res.get("removed")),
        why=res.get("why", ""),
    )


def _merged_branch(repo_dir: Path, pr_id: str | None) -> str:
    """The branch name of the PR that was just merged.

    Read from GitHub, from the PR itself — `headRefName` is the branch the PR
    was opened from, which is exactly the branch whose worktree we want to
    remove. The local HEAD is NOT a substitute: it is the branch the MERGE ran
    from, usually the trunk, and asking for the trunk's worktree would either
    do nothing (the right outcome by accident) or try to remove the repository
    itself (the wrong outcome). A cleanup that targets the wrong branch is
    worse than no cleanup, because it looks like it worked.

    The remote head branch is deleted by the merge itself, but `headRefName`
    survives the deletion: GitHub keeps the PR record.

    An empty answer means "unknown", never "no worktree".
    """
    name = _pr_head_branch(repo_dir, pr_id)
    if name:
        return name
    return ""


def _pr_head_branch(repo_dir: Path, pr_id: str | None) -> str:
    """`gh pr view --json headRefName` — the branch the PR was opened from."""
    cmd = ["gh", "pr", "view"]
    if pr_id:
        cmd.append(str(pr_id))
    cmd.extend(["--json", "headRefName", "--jq", ".headRefName"])
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()
