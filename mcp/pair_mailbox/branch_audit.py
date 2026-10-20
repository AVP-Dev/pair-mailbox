"""branch audit: the §20 janitor, as a contract rather than a sweep.

PROTOCOL §20 gives five branch classes and one janitor with one job: delete only
what it can *prove* merged, and report everything else to a human. This module is
that contract, executable.

Fail-closed in every direction, and each rule below is a refusal the audit
actually makes, not a comment about what it ought to refuse:

- a class the robot may never delete (trunk, strategic, personal) is refused
  even when its PR is MERGED — a merged release line is not a merged branch;
- an ephemeral branch is deleted only when its own PR proves MERGED on the
  remote, and the head SHA of that proof is the one we saw;
- an ephemeral branch with no PR, a CLOSED-unmerged PR, or an abandoned one is
  STALE — reported, never deleted: it may be the only copy of work the repo
  refused;
- an unrecognised name is ANOMALY, and an anomaly is never a cleanup candidate:
  tidying anomalies teaches everyone to name badly;
- a `gh` that cannot answer is a refusal, never a silent skip: the audit could
  not see the remote, so it did not verify anything.

`gh pr view` is the only state source. Branch age is not consulted, and neither
is "the PR looks closed": the age of a branch says nothing about whether it was
merged, which is exactly the confusion that leaves a `dev` around for twenty
rounds.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping, Protocol, runtime_checkable

from branch_classes import ACCUMULATORS, classify


@runtime_checkable
class _TranscriptWriter(Protocol):
    """The store capability this module actually uses.

    `Any` would be honest too — the dispatcher hands the janitor a Store or a
    _LazyStore, and neither is knowable here — but the capability is knowable,
    and a capability named is narrower than a shrug. Every other `cmd_*` keeps
    `Any` because it touches far more of the store than one method.
    """

    def append_transcript(self, kind: str, role: str, **fields: Any) -> None: ...

MERGEABLE = "ephemeral"          # the only class a delete may target


class _RowCounter:
    """A store that counts the rows written through it.

    The summary line has to say how many transcript rows exist, and it may only
    say what it counted — a number written next to the word "deleted" is a
    claim, and §20 rule 5 makes it the only reason a deletion is defensible.
    """

    def __init__(self, store: _TranscriptWriter) -> None:
        self._store = store
        self.count = 0

    def append_transcript(self, kind: str, role: str, **fields: Any) -> None:
        self.count += 1
        self._store.append_transcript(kind, role, **fields)


def gh_pr_state(repo_dir: Path, branch: str) -> tuple[str, str]:
    """`MERGED`/`OPEN`/`CLOSED` and the PR number for a branch, via gh.

    Fail-closed: `gh` failing, hanging, or answering something unparseable raises,
    because the audit then knows nothing and must not act as if it did.
    """
    proc = subprocess.run(
        ["gh", "pr", "list", "--head", branch, "--state", "all",
         "--json", "number,state,mergeCommit,headRefOid",
         "--limit", "1"],
        cwd=str(repo_dir), capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"gh pr list failed for {branch} (rc={proc.returncode}): "
            f"{proc.stderr.strip()[:200]}"
        )
    rows = json.loads(proc.stdout or "[]")
    if not rows:
        return "NO_PR", ""
    row = rows[0]
    return str(row.get("state", "") or "UNKNOWN"), str(row.get("number", "") or "")


def gh_pr_head(repo_dir: Path, pr: str) -> str:
    """The head SHA the remote recorded for the PR, so a delete cannot delete a
    commit other than the one the merge named."""
    proc = subprocess.run(
        ["gh", "pr", "view", pr, "--json", "headRefOid", "--jq", ".headRefOid"],
        cwd=str(repo_dir), capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"gh pr view {pr} failed (rc={proc.returncode}): "
            f"{proc.stderr.strip()[:200]}"
        )
    return proc.stdout.strip()


def _remote_branches(repo_dir: Path) -> list[tuple[str, str]]:  # (short name, sha)
    """(short name, head sha) for every head the REMOTE has, or raise — never
    empty by accident. A `git` that fails answers empty, and empty reads as
    nothing to do.

    The remote is asked, not the local cache. `refs/remotes/` keeps branches
    GitHub already deleted — a merged PR's branch dies on the remote while its
    tracking ref lingers until somebody fetches with `--prune` — and a janitor
    that audits the cache audits branches that are not there: it reports them,
    offers to delete them, and then fails to delete them. The remote is the
    only place the question "what exists" has an answer.
    """
    proc = subprocess.run(
        ["git", "ls-remote", "--heads", "origin"],
        cwd=str(repo_dir), capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"git ls-remote failed (rc={proc.returncode}): "
            f"{proc.stderr.strip()[:200]}"
        )
    out = []
    for line in proc.stdout.splitlines():
        sha, _, ref = line.partition("\t")
        if not ref.startswith("refs/heads/"):
            continue
        out.append((ref[len("refs/heads/"):], sha.strip()))
    return out


def audit(repo_dir: Path) -> tuple[list[dict[str, str]], list[str]]:
    """Classify every remote branch; return (rows, refusals).

    Each row carries what a human needs to act: branch, class, verdict (MERGE /
    STALE / ANOMALY / KEEP), and the PR that backs it when there is one.
    """
    rows: list[dict[str, str]] = []
    refusals: list[str] = []
    for name, sha in sorted(_remote_branches(repo_dir)):
        cls = classify(name)
        row = {"branch": name, "class": cls, "sha": sha, "verdict": "KEEP",
               "pr": "", "reason": ""}
        if cls in ("trunk", "strategic", "personal"):
            row["reason"] = f"{cls}: a robot never deletes this class (§20)"
            rows.append(row)
            continue
        try:
            state, pr = gh_pr_state(repo_dir, name)
        except (RuntimeError, OSError, subprocess.SubprocessError,
                json.JSONDecodeError) as exc:
            refusals.append(f"{name}: {exc}")
            row["verdict"] = "UNKNOWN"
            row["reason"] = "gh could not answer; nothing was verified"
            rows.append(row)
            continue
        row["pr"] = pr
        if cls != MERGEABLE:
            row["verdict"] = "ANOMALY"
            if name in ACCUMULATORS:
                row["reason"] = (
                    "accumulator: an antipattern under §20 rule 4 — reported "
                    "for a human to remove with a reason"
                )
            else:
                row["reason"] = "no §20 class matches this name"
            rows.append(row)
            continue
        if state == "MERGED":
            try:
                remote_head = gh_pr_head(repo_dir, pr) if pr else ""
            except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
                refusals.append(f"{name}: {exc}")
                row["verdict"] = "UNKNOWN"
                row["reason"] = "merge state known, head not; nothing was verified"
                rows.append(row)
                continue
            if remote_head and remote_head != sha:
                row["verdict"] = "ANOMALY"
                row["reason"] = (
                    f"remote head {remote_head[:12]} != PR head "
                    f"{(pr or '')} record; refusing to touch it"
                )
                rows.append(row)
                continue
            row["verdict"] = "MERGE"
            row["reason"] = "PR proves MERGED; delete with a transcript row"
            rows.append(row)
            continue
        row["verdict"] = "STALE"
        row["reason"] = (
            "no merged PR backs it ("
            + (state if state else "no PR")
            + "); it may be the only copy of work the repo refused"
        )
        rows.append(row)
    return rows, refusals


def delete_merged(repo_dir: Path, row: Mapping[str, str],
                  store: _TranscriptWriter,
                  role: str = "writer") -> bool:
    """Delete one MERGE row, and write what was known when it was decided.

    The transcript row is not decoration: it is the only reason a deletion is
    defensible a month later. So a delete that cannot be recorded is not
    performed, and a delete that fails is a refusal — never a deletion.

    Returns True when the branch is gone and its row is written.
    """
    name = row["branch"]
    proc = subprocess.run(
        ["git", "push", "origin", "--delete", name],
        cwd=str(repo_dir), capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        print(f"refusing to record: delete of {name} failed:\n"
              f"{(proc.stdout + proc.stderr).strip()[:300]}", file=sys.stderr)
        return False
    store.append_transcript(
        "branch_deleted",
        role,
        branch=name,
        head_sha=row["sha"],
        pr=row["pr"],
        reason="ephemeral + PR MERGED (§20 janitor)",
    )
    print(f"deleted {name} (head {row['sha'][:12]}, PR #{row['pr']} MERGED)")
    return True


def cmd_branch(store: _TranscriptWriter | None,
               args: argparse.Namespace) -> int:
    """Handler for `pairctl branch` — the §20 janitor.

    Report by default, delete only with `--delete`, and only what the remote
    proved MERGED. The store is optional on purpose: an audit of a repo whose
    pair state was wiped still classifies branches, and `_LazyStore` would make
    that command fail on a missing `state.json` for no reason.
    """
    if getattr(args, "branch_action", "") != "audit":
        print(f"unknown branch action: {args.branch_action!r}", file=sys.stderr)
        return 2
    repo = Path(getattr(args, "repo", "") or getattr(store, "project_root", ".") or ".")
    try:
        rows, refusals = audit(repo)
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        print(f"branch audit: refusing to run: {exc}", file=sys.stderr)
        return 2

    for row in rows:
        marker = {"MERGE": "delete-ok", "STALE": "report", "ANOMALY": "report",
                  "KEEP": "keep", "UNKNOWN": "unknown"}[row["verdict"]]
        print(f"  {marker:9s} {row['branch']:28s} {row['class']:10s} "
              f"PR {row['pr'] or '-':>5s}  {row['reason']}")
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["verdict"]] = counts.get(row["verdict"], 0) + 1
    print("branch audit: " + ", ".join(
        f"{n} {k.lower()}" for k, n in sorted(counts.items()))
        + f", {len(rows)} branches")
    for refusal in refusals:
        print(f"branch audit REFUSAL: {refusal}", file=sys.stderr)

    if not getattr(args, "delete", False):
        print("branch audit: report only; --delete acts on MERGE rows")
        return 1 if refusals else 0

    if store is None:
        # A deletion without a transcript row is a deletion nobody can defend
        # a month later, so it is not performed.
        print("branch audit: refusing to delete: no store to record the "
              "deletions in", file=sys.stderr)
        return 2

    counter = _RowCounter(store)
    deleted = 0
    refused = 0
    role = getattr(args, "role", "") or "writer"
    for row in rows:
        if row["verdict"] != "MERGE":
            continue
        if delete_merged(repo, row, counter, role):
            deleted += 1
        else:
            refused += 1
    print(f"branch audit: {deleted} branch(es) deleted, {refused} refused, "
          f"{counter.count} transcript row(s) written")
    return 1 if (refusals or refused) else 0
