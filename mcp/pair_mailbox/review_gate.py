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


def check_pr_merge(store: Any, repo_dir: Path) -> tuple[bool, str]:
    """Check whether the PR merge passes the protocol rules.

    Returns (True, "") on a passed check or (False, refusal_reason).
    """
    head = review.git_head(repo_dir)
    if not head:
        return False, "cannot verify: not a git repo"

    state = store.read()
    phase = state.get("phase")
    if phase != "verify":
        return False, f"phase is {phase!r}, PR merge allowed strictly in 'verify'"

    # Check for successful test evidence in the current round
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
        last_block = blocks[-1]
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
        if VERDICT_REJECT_RE.search(last_block):
            return False, "last verdict rejected the changes (REFUSE / REJECT / NOT APPROVED)"

        approve_match = VERDICT_APPROVE_RE.search(last_block)
        if not approve_match:
            return False, "last verdict is not approved (no structured 'verdict-approve <head|round>' marker)"

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
    """Run the merge through the gh cli."""
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
    """Handler for the `pairctl pr` subcommands."""
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
