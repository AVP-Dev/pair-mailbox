"""Round manifest: server-derived JSON of what a round did (.pair/rounds/<N>.json).

Why a manifest, not the receipt. The receipt is a human page; the manifest is
machine data: every claim as structure (claim + method + result), the negative
record (what was NOT covered), and the hashes to re-derive it later. Agents
never write it — the server builds it from state/transcript/git at close, and
bin/pairreport only reads it.

The close-written manifest is the CLAIM; `pairreport --recheck` re-derives the
CURRENT tree; the printed delta is what changed after the claim, and what the
claim got wrong.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any


class ManifestRefused(Exception):
    """The journal disagrees with the manifest: fail closed with a reason."""


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _git_commits(project_root: Path, since_round_start: str = "") -> list[dict[str, str]]:
    """Commits on the current branch: sha + subject. Newest last."""
    try:
        out = subprocess.run(["git", "-C", str(project_root), "log",
                              "--format=%H %s", "master..HEAD"],
                             capture_output=True, text=True, timeout=15).stdout or ""
    except Exception:
        return []
    commits = []
    for line in out.splitlines():
        if not line.strip():
            continue
        sha, _, subject = line.partition(" ")
        commits.append({"sha": sha[:12], "subject": subject[:120]})
    return commits


def _transcript_paths(state_dir: Path) -> list[Path]:
    """Active journal plus rotated archives, oldest first.

    Rotation shifts the active file to transcript-archive/1.jsonl (server.py
    _rotate_transcript, TRANSCRIPT_ARCHIVES = 4). Readers that see only the
    active file go blind the moment rotation cuts it — round 94 closed with
    tasks_closed [] while task_closed 107-110 sat in the archive. Same
    precedent as scripts/eye-of-sauron.sh.
    """
    archive = Path(state_dir) / "transcript-archive"
    try:
        archived = list(archive.glob("*.jsonl"))
    except OSError:
        archived = []
    # Numeric order, oldest rotation first: N..2,1 then active. Numeric sort,
    # not lexical ("10" < "2" lexically). Higher number = older rotation
    # (each shift bumps 1->2, 2->3; the freshest cut lands at 1.jsonl).
    def _num(p: Path) -> int:
        try:
            return int(p.stem)
        except ValueError:
            return -1
    archived = sorted(archived, key=_num, reverse=True)
    return archived + [state_dir / "transcript.jsonl"]


def _transcript_rows(state_dir: Path) -> list[dict[str, Any]]:
    rows = []
    for path in _transcript_paths(state_dir):
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for raw in text.splitlines():
            if not raw.strip():
                continue
            try:
                rows.append(json.loads(raw))
            except ValueError:
                continue
    return rows


def _round_bounds(state_dir: Path, round_number: int, state: dict[str, Any]) -> tuple[str, str | None]:
    """Round N's own window: (anchor, ceiling) over the united stream.

    Stamped rows of N: evidence rows tagged round=N in the stream plus
    closed_at of state tasks labeled N (closed_round, else add-round).
    task_closed rows carry no round tag, so they are NOT inputs — they are
    what the window CHECKS. Anchor: last done-advance at/before the first
    stamped row (the done that closed N-1); \"\" when none. Ceiling: first
    done-advance at/after the last stamped row (the done that closed N);
    None while N is still open (window runs to now) or when the closing
    advance is gone from the surviving stream (archive rotated past it,
    TRANSCRIPT_ARCHIVES = 4) — callers print cannot-prove, never empty.
    """
    rows = _transcript_rows(state_dir)
    dones = sorted(str(r.get("ts", "")) for r in rows
                   if r.get("kind") == "advance" and r.get("phase") == "done")
    stamped = sorted(str(r.get("ts", "")) for r in rows
                     if r.get("kind") == "evidence" and int(r.get("round", -1)) == round_number)
    for t in (state.get("tasks", []) or []):
        labeled = (int(t["closed_round"]) == round_number if t.get("closed_round") is not None
                   else int(t.get("round", -1)) == round_number)
        if labeled and t.get("closed_at"):
            stamped.append(str(t["closed_at"]))
    stamped.sort()
    if not stamped:
        return (dones[-1] if dones else ""), None
    anchor = max([d for d in dones if d <= stamped[0]] + [""])
    after = [d for d in dones if d >= stamped[-1]]
    return anchor, (after[0] if after else None)

def _round_merged(state: dict[str, Any], project_root: Path, round_number: int) -> bool | None:
    """Whether round N is MERGED, three-valued (round 103 S1).

    Two independent sources, in order: recorded commit evidence of this round
    naming an origin/master advance (the writer's own record, never trusted
    alone — see below), then git: the evidence tip is reachable from a
    remote-tracking master ref (LOCAL probe only — never `gh`/network; this
    runs in derive, which callers run on hot paths).

    Fail direction: unknown (no evidence, git broken) returns None and callers
    treat None as NOT merged — an unrecorded merge must look open, never the
    reverse: scoring an unshipped round as merged is the round-102 lie this
    function exists to refuse.
    """
    import re
    import subprocess

    def _run_git(*args: str) -> int | None:
        try:
            proc = subprocess.run(["git", *args], cwd=str(project_root),
                                  capture_output=True, timeout=5)
        except Exception:  # noqa: BLE001 - broken git is unknown, never merged
            return None
        return proc.returncode

    tip: str | None = None
    for e in (state.get("evidence") or []):
        if int(e.get("round", -1)) != round_number:
            continue
        if e.get("evidence_kind") not in ("commit",) and e.get("kind") not in ("commit",):
            continue
        value = str(e.get("value", ""))
        # Only an origin/master ADVANCE admits a merge: "origin/master X..Y"
        # names the tip Y; "origin/master advanced X -> Y" (merge-commit
        # evidence row, round 102's real shape) names the new tip Y. A bare
        # sha (the branch-push row) does not.
        for form in (r"origin/master\s+[0-9a-f]{7,40}\.\.[0-9a-f]{7,40}",
                     r"origin/master\s+advanced\s+([0-9a-f]{7,40})\s*->\s*([0-9a-f]{7,40})"):
            for match in re.findall(form, value):
                tip = match[1] if isinstance(match, tuple) else str(match).rsplit("..", 1)[1].split()[-1]
                break
            if tip:
                break
        if tip:
            break
    if not tip:
        return None
    for ref in ("refs/remotes/origin/master", "refs/heads/master"):
        rc = _run_git("merge-base", "--is-ancestor", tip, ref)
        if rc == 0:
            return True
        if rc == 1:
            continue  # this ref does not reach it; try the other
        return None  # broken git: unknown
    return False


def _round_commits_from_evidence(state: dict[str, Any], round_number: int) -> list[dict[str, str]] | None:
    """Closed round's commits from its recorded merge evidence, never the live diff.

    Returns None when the round recorded no commit evidence: the commits are
    then UNRECOVERABLE (not empty) — callers print cannot-prove, never [].
    Parses the real writer strings: full sha + master range
    ("PR #40 merged: 771fd77 ... origin/master b6bfb65..771fd77").
    A bare kind=pr row (URL only, no tip — round 89's shape) names nothing.
    """
    import re
    out: list[dict[str, str]] = []
    seen: set[str] = set()

    def _add(sha: str, value: str) -> None:
        short = sha[:12]
        if short not in seen:
            seen.add(short)
            out.append({"sha": short, "subject": value[:120]})

    for e in (state.get("evidence") or []):
        if int(e.get("round", -1)) != round_number:
            continue
        if e.get("evidence_kind") not in ("commit",) and e.get("kind") not in ("commit",):
            continue
        value = str(e.get("value", ""))
        ranged = re.findall(r"[0-9a-f]{7,40}\.\.[0-9a-f]{7,40}", value)
        if ranged:
            # "origin/master X..Y" names Y only: X is the pre-merge anchor,
            # a commit the round did not make.
            for span in ranged:
                _add(span.split("..")[1], value)
            continue
        for sha in re.findall(r"\b[0-9a-f]{7,40}\b", value):
            _add(sha, value)
    return out or None


def _note_hashes(state_dir: Path, kinds: tuple[str, ...] = ("plan",)) -> list[dict[str, str]]:
    """Draft paths + content hashes for the miting drafts (derived, not typed)."""
    out = []
    notes = state_dir / "notes"
    if not notes.is_dir():
        return out
    for path in sorted(notes.glob("*.md")):
        stem = path.stem
        if not any(stem == k or stem.startswith(k + "-") or stem.startswith(k.replace("-", "")) or k in stem
                   for k in kinds):
            continue
        try:
            out.append({"path": f".pair/notes/{path.name}", "sha": _sha(path.read_text(encoding="utf-8"))})
        except OSError:
            continue
    return out


def derive_manifest(state_dir: Path, state: dict[str, Any], round_number: int | None = None) -> dict[str, Any]:
    """Build the manifest from state/transcript/git. Pure read, never writes.

    round_number selects whose window and commits are derived: the write path
    passes the current round, recheck passes --round N. Default (None) keeps
    the current round from state. A closed round derives its OWN window
    (anchor..ceiling) and its commits from recorded merge evidence — never
    the live master..HEAD, which is empty after the merge.
    """
    state_dir = Path(state_dir)
    round_number = int(state.get("round", 0)) if round_number is None else round_number
    tasks = state.get("tasks", [])
    closed_here = tasks_closed_in_round(tasks, round_number,
                                        transcript_closed_ids(state_dir, round_number, tasks, state))
    evidences = [e for e in (state.get("evidence") or [])
                 if int(e.get("round", -1)) == round_number]
    anchor, ceiling = _round_bounds(state_dir, round_number, state)
    # M1: state verdicts carry at+note — filter by at inside N's window,
    # not a rebind: the note path rides along untouched.
    verdicts = [v for v in (state.get("verdicts", []) or [])
                if str(v.get("at", "")) >= anchor and (ceiling is None or str(v.get("at", "")) < ceiling)]
    # Negative record + method: verdict notes carry NOT-covered sections and
    # HOW each claim was verified in prose (round 94: no separate structure
    # exists in state). The manifest binds each verdict to its note path + hash
    # — the pointer, not a retyped copy. A manifest claiming verdicts with no
    # note behind them is the lie the cross-check refuses.
    verdict_notes = _verdict_note_hashes(state_dir)
    rows = _transcript_rows(state_dir)
    project_root = state_dir.parent
    counter_match = round_number == int(state.get("round", 0))
    if not counter_match:
        is_open = False  # a past round advances away from itself: closed, never re-derived
    else:
        # Round 103 S1: the round 102 lie — manifest written AFTER the merge,
        # state.round still == 102, old predicate kept the live branch (empty
        # after the merge) and recorded commits [] with the merge evidence
        # naming six shas, cross_check silent. Open now requires BOTH: this is
        # still the state's round AND no origin/master advance is recorded.
        # Confirmed merged = recorded advance whose tip is reachable from the
        # local master ref (three-valued; see _round_merged).
        is_open = _round_merged(state, project_root, round_number) is not True
    if is_open:
        commits: list[dict[str, str]] | None = _git_commits(project_root)
        commits_unrecoverable = False
    else:
        # Closed round: the live master..HEAD is empty after the merge —
        # commits come from the recorded merge evidence, or None
        # (UNRECOVERABLE, never []) when the writer recorded none.
        commits = _round_commits_from_evidence(state, round_number)
        commits_unrecoverable = commits is None
    plan_path = state_dir / "plan.md"
    try:
        plan_sha = _sha(plan_path.read_text(encoding="utf-8")) if plan_path.exists() else ""
    except OSError:
        plan_sha = ""
    return {
        "round": round_number,
        "mode": state.get("mode", "fix"),
        "writer": state.get("writer", ""),
        "request": [t.get("text", "") for t in closed_here if t.get("status") == "done"],
        "drafts": _note_hashes(state_dir, ("plan",)),
        "plan": {"path": ".pair/plan.md", "sha": plan_sha},
        "commits": commits,
        "commits_unrecoverable": commits_unrecoverable,
        "tasks_closed": [{"id": t.get("id"), "text": (t.get("text") or "")[:120],
                          "status": t.get("status"),
                          "bound_by": t.get("bound_by", "")} for t in closed_here],
        "tasks_dropped": [{"id": t.get("id"), "reason": (t.get("reason") or "")[:120]}
                          for t in closed_here if t.get("status") == "dropped"],
        "verdicts": [{"role": v.get("role"), "kind": v.get("kind"), "at": v.get("at"),
                      "note": _note_for_kind(verdict_notes, str(v.get("kind", "")))}
                     for v in verdicts],
        "negative": _negative_lines(state_dir, [str(v.get("kind", "")) for v in verdicts]),
        "evidence": [_evidence_row(e) for e in evidences],
        "transcript_kinds": _count_kinds(rows),
    }


def _verdict_note_hashes(state_dir: Path) -> list[dict[str, str]]:
    """Verdict note paths + hashes: the negative record lives in these notes."""
    out = []
    notes = state_dir / "notes"
    if not notes.is_dir():
        return out
    for path in sorted(notes.glob("verdict*.md")):
        try:
            out.append({"kind": path.stem, "path": f".pair/notes/{path.name}",
                        "sha": _sha(path.read_text(encoding="utf-8"))})
        except OSError:
            continue
    return out


_METHOD_MARKERS = ("re-run", "rerun", "mutation", "dry-run", "live run",
                     "full gate", "read", "independent", "green")


def _evidence_row(item: dict[str, Any]) -> dict[str, Any]:
    """Claim + method + result. The method is derived from markers in the
    recorded value/note — "reviewed" with no marker reads unstated, never assumed."""
    text = ((item.get("value") or "") + " " + (item.get("note") or "")).lower()
    methods = [m for m in _METHOD_MARKERS if m in text]
    return {"kind": item.get("kind"), "ok": item.get("ok"),
            "value": (item.get("value") or "")[:200], "role": item.get("role"),
            "method": methods or ["unstated"]}


def tasks_closed_in_round(tasks: list[dict[str, Any]], round_number: int,
                          transcript_ids: set[int] | None = None) -> list[dict[str, Any]]:
    """Bind tasks to a round with the rule labeled on each task.

    closed_round (post-D2) → add-round fallback (pre-D2, labeled) →
    transcript window (stale-server closes, labeled). The label rides in
    BOTH the manifest and the receipt so the pages agree.
    """
    out = []
    for task in tasks:
        bound_by = ""
        if task.get("closed_round") is not None:
            if int(task["closed_round"]) == round_number:
                bound_by = "closed_round"
            else:
                continue
        elif int(task.get("round", -1)) == round_number:
            bound_by = "add-round (pre-D2)"
        elif transcript_ids is not None and int(task.get("id", 0)) in transcript_ids:
            bound_by = "transcript (stale-server close)"
        else:
            continue
        row = dict(task)
        row["bound_by"] = bound_by
        out.append(row)
    return out


def transcript_closed_ids(state_dir: Path, round_number: int | None = None,
                          tasks: list[dict[str, Any]] | None = None,
                          state: dict[str, Any] | None = None) -> set[int]:
    """Task ids with task_closed in round N's OWN window (anchor..ceiling).

    Round is required: the old no-arg form answered "since last done",
    which after round N closes is N's own close — every closed round then
    re-derived []. The window comes from N-STAMPED rows (evidence round=N
    plus closed_at of N-labeled tasks); every task_closed inside it is
    reported, not just pre-bound ids — the binding is what is checked.
    """
    if round_number is None or tasks is None:
        return {int(r.get("task_id", 0)) for r in _transcript_rows(state_dir)
                if r.get("kind") == "task_closed" and _in_round_window(state_dir, r)}
    anchor, ceiling = _round_bounds(state_dir, round_number, state or {"tasks": tasks})
    # A task_closed row carries no round tag, so the window alone mis-binds
    # tasks closed AFTER their round's `done` — round 103's #121-123 were
    # closed at 10:49-11:49, after round 104 had already opened at 08:52, and
    # landed in 104's window. The task's own closed_round (its state record)
    # decides membership; the window stays as the outer bound.
    by_id = {int(t.get("id", 0)): t for t in (tasks or [])}
    out = set()
    for r in _transcript_rows(state_dir):
        if r.get("kind") != "task_closed":
            continue
        ts = str(r.get("ts", ""))
        if not (ts >= anchor and (ceiling is None or ts < ceiling)):
            continue
        tid = int(r.get("task_id", 0))
        item = by_id.get(tid)
        if item is not None:
            own = item.get("closed_round", item.get("round"))
            if own is not None and int(own) != round_number:
                continue
        out.add(tid)
    return out


def _in_round_window(state_dir: Path, row: dict[str, Any]) -> bool:
    """Whether a transcript row falls in the current round's window.

    Legacy open-round form (ts >= last done): kept for the receipt path and
    any caller without a round number. Round-scoped derivations use
    _round_bounds + transcript_closed_ids(state_dir, N, tasks) instead —
    after round N closes, \"last done\" IS N's own close and this form
    re-derives [].

    The window runs over the UNITED stream (active plus transcript-archive):
    rotation cuts the active file mid-round, so a file-local last-done would
    restart the window at the cut. Honest caveat: the archive itself rotates
    (TRANSCRIPT_ARCHIVES = 4, server.py:197) — a round older than the surviving archive cannot prove
    its window, and the check then leans open rather than refusing on gaps
    it cannot see.
    """
    try:
        rows = _transcript_rows(state_dir)
        dones = [r.get("ts", "") for r in rows
                 if r.get("kind") == "advance" and r.get("phase") == "done"]
        if dones:
            return str(row.get("ts", "")) >= max(dones)
        reopens = [r.get("ts", "") for r in rows
                   if r.get("kind") == "advance" and r.get("reopened")]
        if not reopens:
            return True
        return str(row.get("ts", "")) >= max(reopens)
    except Exception:
        return True


def _note_for_kind(notes: list[dict[str, str]], kind: str) -> str:
    for note in notes:
        if note["kind"] == kind:
            return note["path"]
    return ""


_NEGATIVE_MARKERS = ("NOT cover", "NOT prove", "does NOT", "not covered",
                     "out of scope", "leaves out", "stays quiet", "remains before",
                     "не покрывает", "не проверен", "не проверяе", "вне скоупа",
                     "упущен", "осталось", "не сделано")


def _negative_lines(state_dir: Path, kinds: list[str]) -> list[dict[str, str]]:
    """Aggregate NOT-covered lines from verdict notes: kind + file:line + text.

    The human's omissions question answered as pointers, not prose-hunting.
    """
    out = []
    notes = state_dir / "notes"
    if not notes.is_dir():
        return out
    for path in sorted(notes.glob("verdict*.md")):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for number, line in enumerate(lines, 1):
            if any(marker in line for marker in _NEGATIVE_MARKERS):
                out.append({"kind": path.stem,
                            "where": f".pair/notes/{path.name}:{number}",
                            "text": line.strip()[:160]})
    return out


def _count_kinds(rows: list[dict[str, Any]]) -> dict[str, int]:
    kinds: dict[str, int] = {}
    for row in rows:
        key = str(row.get("kind", "?"))
        kinds[key] = kinds.get(key, 0) + 1
    return kinds


def cross_check(manifest: dict[str, Any], state: dict[str, Any],
                state_dir: Path) -> list[str]:
    """Write-time cross-check: structural gaps refuse, never partial manifests.

    Only empty-list-with-nonempty-source refuses — never byte equality, or the
    race between derive and write would false-refuse.
    """
    gaps = []
    round_number = manifest.get("round", 0)
    # Round 103 S1: the branch is picked on the SAME predicate as the derive
    # (counter past + non-merged today). This is the load-bearing call site:
    # a merged round must never take the live-branch branch, or it records
    # commits [] forever while its merge evidence names the commits (round 102
    # crossed the gate exactly so). Unknown (None) reads as OPEN here: an
    # unverified merge stays guarded by the live-branch check, never scored
    # closed on a guess.
    counter_match = round_number == int(state.get("round", 0))
    if not counter_match:
        is_open = False
    else:
        is_open = _round_merged(state, state_dir.parent, round_number) is not True
    if is_open:
        # OPEN round (C4): the live branch question stays — this is what
        # protects the round being closed right now. Must stay red-proven.
        if manifest.get("commits") == [] and _git_commits(state_dir.parent):
            gaps.append("commits present on branch but manifest lists none")
    else:
        # CLOSED round (B1): live master..HEAD is empty by design after a
        # merge. Recorded merge evidence is the anchor; [] or None with an
        # anchor refuses — and None (unrecoverable) with rows in the window
        # refuses too, because a window with rows and no commits is the same
        # lie wearing a different hat. None collapses to [] NOWHERE.
        anchor = _round_commits_from_evidence(state, round_number)
        claimed_commits = manifest.get("commits")
        if claimed_commits in ([], None) and anchor:
            gaps.append("recorded merge evidence names commits but manifest lists none")
        elif claimed_commits is None and transcript_closed_ids(
                state_dir, round_number, state.get("tasks", []), state):
            gaps.append("window holds closed tasks but manifest commits unrecoverable — "
                        "record the merge evidence or state cannot-prove")
    anchor, ceiling = _round_bounds(state_dir, round_number, state)
    verdict_notes = [r for r in _transcript_rows(state_dir) if str(r.get("kind", "")).startswith("verdict")
                     or r.get("kind") == "note" and str(r.get("note_kind", "")).startswith("verdict")]
    verdict_notes = [r for r in verdict_notes
                     if str(r.get("ts", "")) >= anchor and (ceiling is None or str(r.get("ts", "")) < ceiling)]
    if manifest.get("verdicts") == [] and verdict_notes:
        gaps.append("verdict notes in journal but manifest lists none")
    # Tasks from the TRANSCRIPT in round N's OWN window, not from state's
    # list: the binding is what is in question (round 94: four task_closed
    # since the claim, manifest would claim []).
    closed_ids = transcript_closed_ids(state_dir, round_number, state.get("tasks", []), state)
    claimed_ids = {int(x.get("id", 0)) for x in (manifest.get("tasks_closed") or [])
                   if isinstance(x, dict)}
    if closed_ids - claimed_ids:
        gaps.append("journal closes tasks "
                    + ",".join(f"#{i}" for i in sorted(closed_ids - claimed_ids))
                    + " but manifest claims none of them")
    state_evidences = [e for e in (state.get("evidence") or []) if int(e.get("round", -1)) == round_number]
    if manifest.get("evidence") == [] and state_evidences:
        gaps.append("evidence rows in state but manifest lists none")
    return gaps


def write_manifest(state_dir: Path, state: dict[str, Any]) -> Path:
    """Derive, cross-check, write .pair/rounds/<N>.json. Refuses on gaps."""
    state_dir = Path(state_dir)
    manifest = derive_manifest(state_dir, state)
    gaps = cross_check(manifest, state, state_dir)
    if gaps:
        raise ManifestRefused("manifest refused: " + "; ".join(gaps))
    target = state_dir / "rounds" / f"{manifest['round']}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return target
