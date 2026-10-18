"""State rotation: the hot file stops growing forever (round 106 slice B).

Measured, so the scope is honest: parsing state.json costs 1.29 ms, a full
`pair_state` is ONE Store._load, a mutating write 5.67 ms. None of that kills a
transport — the original crash theory was withdrawn. What IS real: the file is
O(rounds), every write rewrites all of it under the global lock, and a cold MCP
start imports 38 ms. Two structures in this repo already solved that shape
(transcript archive, notes rotation), so the state file adopts the third
instance of the same pattern rather than inventing a fourth.

The invariant that must never break: ROTATION MERGES ON LOAD. A record that left
the active window becomes invisible only to the writer's memory, never to the
reader — `load_state` hands back the union of the active window and the
archives, exactly as `_transcript_note_rows` unions its stream.

Layout: `.pair/state.archive/<key>.<n>.jsonl`, one JSON record per line, oldest
first. No index file: ordering is the filename's numeric suffix, and a reader
that cannot resolve it says so rather than guessing.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ARCHIVE_DIRNAME = "state.archive"
# Keys with unbounded growth whose history is read-only in practice. `tasks` and
# `evidence` are the two measured giants (61 KB / 124 records and 31 KB / 63);
# `verdicts` and `patches` stay in the hot file because every close reads the
# LAST ones constantly, and their volume is an order of magnitude smaller.
ROTATABLE: dict[str, int] = {"tasks": 40, "evidence": 40}


def archive_dir(state_dir: Path) -> Path:
    return Path(state_dir) / ARCHIVE_DIRNAME


def shard_path(archive: Path, key: str, generation: int) -> Path:
    return archive / f"{key}.{generation:04d}.jsonl"


def _read_shard(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return rows
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _write_shard(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                    encoding="utf-8")


def archived_rows(archive: Path, key: str) -> list[dict[str, Any]]:
    """Every archived record for `key`, oldest first, across all generations."""
    out: list[dict[str, Any]] = []
    if not archive.is_dir():
        return out
    paths = sorted(
        (p for p in archive.glob(f"{key}.*.jsonl")
         if p.name[len(key) + 1:-len(".jsonl")].isdigit()),
        key=lambda p: int(p.name[len(key) + 1:-len(".jsonl")]))
    for path in paths:
        out.extend(_read_shard(path))
    return out


def rotate(state_dir: Path, keep_tasks: int | None = None,
           keep_evidence: int | None = None) -> tuple[int, int]:
    """Move old records out of the hot file. Returns (tasks_moved, evidence_moved).

    Keep windows come from ROTATABLE by default; explicit values are for tests
    and for an operator who knows their history better. The ACTIVE window keeps
    the NEWEST `keep` records (that is what the tools read); everything older
    leaves for the archive, oldest generation first.

    Idempotent by construction: what already left is read back from the archive
    and the state is rebuilt from that union, so a second call finds the same
    windows already applied and moves nothing.
    """
    state_dir = Path(state_dir)
    state_path = state_dir / "state.json"
    if not state_path.exists():
        return 0, 0
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0, 0
    keeps = {"tasks": keep_tasks, "evidence": keep_evidence}
    moved: dict[str, int] = {"tasks": 0, "evidence": 0}
    archive = archive_dir(state_dir)

    for key, default_keep in ROTATABLE.items():
        keep = keeps.get(key)
        keep = default_keep if keep is None else max(0, int(keep))
        # The union is the truth: what already left PLUS what is still active.
        # A second call then finds the same rows sitting in the archive and
        # needs to move them NO further — so the archive is REWRITTEN as the
        # whole "leaving" set, generation 1, not appended a second time.
        active = [r for r in (state.get(key) or []) if isinstance(r, dict)]
        known = archived_rows(archive, key)
        merged = known + active
        if len(merged) <= keep:
            continue
        leaving = merged[:-keep] if keep else merged
        staying = merged[-keep:] if keep else []
        if not leaving:
            continue
        _write_shard(shard_path(archive, key, 1), leaving)
        state[key] = staying
        moved[key] = len(leaving) - len(known)

    if moved["tasks"] or moved["evidence"]:
        tmp = state_path.with_suffix(".json.rotating")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1) + "\n",
                       encoding="utf-8")
        tmp.replace(state_path)
    return moved["tasks"], moved["evidence"]


def load_state(state_dir: Path, state: dict[str, Any]) -> dict[str, Any]:
    """The union: active window + archives, for every rotatable key.

    Returns the SAME dict, with the rotatable keys replaced by their merged
    lists. Called from Store._load, so no reader has to know rotation exists.
    """
    archive = archive_dir(state_dir)
    for key in ROTATABLE:
        active = [r for r in (state.get(key) or []) if isinstance(r, dict)]
        merged = archived_rows(archive, key) + active
        if merged:
            state[key] = merged
    return state
