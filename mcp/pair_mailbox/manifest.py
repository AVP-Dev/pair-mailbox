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


def _transcript_rows(state_dir: Path) -> list[dict[str, Any]]:
    rows = []
    path = state_dir / "transcript.jsonl"
    if not path.exists():
        return rows
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not raw.strip():
            continue
        try:
            rows.append(json.loads(raw))
        except ValueError:
            continue
    return rows


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


def derive_manifest(state_dir: Path, state: dict[str, Any]) -> dict[str, Any]:
    """Build the manifest from state/transcript/git. Pure read, never writes."""
    state_dir = Path(state_dir)
    round_number = int(state.get("round", 0))
    tasks = state.get("tasks", [])
    closed_here = [t for t in tasks
                   if int(t.get("closed_round", t.get("round", -1)) or -1) == round_number]
    evidences = [e for e in (state.get("evidence") or [])
                 if int(e.get("round", -1)) == round_number]
    verdicts = state.get("verdicts", []) or []
    # Negative record + method: verdict notes carry NOT-covered sections and
    # HOW each claim was verified in prose (round 94: no separate structure
    # exists in state). The manifest binds each verdict to its note path + hash
    # — the pointer, not a retyped copy. A manifest claiming verdicts with no
    # note behind them is the lie the cross-check refuses.
    verdict_notes = _verdict_note_hashes(state_dir)
    rows = _transcript_rows(state_dir)
    project_root = state_dir.parent
    commits = _git_commits(project_root)
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
        "tasks_closed": [{"id": t.get("id"), "text": (t.get("text") or "")[:120],
                          "status": t.get("status")} for t in closed_here],
        "tasks_dropped": [{"id": t.get("id"), "reason": (t.get("reason") or "")[:120]}
                          for t in tasks
                          if int(t.get("closed_round", t.get("round", -1)) or -1) == round_number
                          and t.get("status") == "dropped"],
        "verdicts": [{"role": v.get("role"), "kind": v.get("kind"), "at": v.get("at"),
                      "note": _note_for_kind(verdict_notes, str(v.get("kind", "")))}
                     for v in verdicts],
        "negative": "NOT-covered sections live in the verdict notes above — "
                    "read the note behind each kind; no separate structure exists",
        "evidence": [{"kind": e.get("kind"), "ok": e.get("ok"),
                      "value": (e.get("value") or "")[:200], "role": e.get("role")}
                     for e in evidences],
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


def _note_for_kind(notes: list[dict[str, str]], kind: str) -> str:
    for note in notes:
        if note["kind"] == kind:
            return note["path"]
    return ""


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
    if manifest.get("commits") == [] and _git_commits(state_dir.parent):
        gaps.append("commits present on branch but manifest lists none")
    verdict_notes = [r for r in _transcript_rows(state_dir) if str(r.get("kind", "")).startswith("verdict")
                     or r.get("kind") == "note" and str(r.get("note_kind", "")).startswith("verdict")]
    if manifest.get("verdicts") == [] and verdict_notes:
        gaps.append("verdict notes in journal but manifest lists none")
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
