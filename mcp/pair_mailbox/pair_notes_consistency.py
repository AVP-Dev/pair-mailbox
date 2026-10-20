"""Notes consistency: every note entry must exist in the journal (round 105 slice 4b).

A note is two writes that must agree: the entry in notes/<kind>.md (written by
hand or by the appender) and the transcript row pair_note appends beside it —
same role, same note_sha, same attempt. A note that only one side knows is
invisible to the protocol: the digest/review paths read the journal, the gate
reads the file, and the kind-file policy (never write into another agent's file)
is unenforceable if the file's provenance cannot be checked.

This module is that check. It never edits, never repairs, never deletes: it
NAMES what does not match, so the writer sees their own hand-edit rather than
discovering it when a reviewer's gate refuses.

Entry format (written by pair_note, server.py:3518):
    ## <ts> — <role> (note <sha12> attempt <N>)

Called from the close path and the verify path, so an orphaned entry blocks a
round instead of living in the tree unnoticed until round 106's machine leg.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

# `## <ts> — <role> (note <sha12> attempt <N>)` — the header pair_note writes.
# The sha group is hex, 12 chars, exactly what `_hashlib.sha256(...)[:12]` gives.
NOTE_ENTRY_RE = re.compile(
    r"^##\s*(\S+)\s+—\s+(\S+)(?:\s*\(note\s+([0-9a-f]{12})\s+attempt\s+(\d+)\s*\))?\s*$"
)

# The timestamp is an ISO-ish stamp wherever it sits: canonical headers put it
# first, corrected headers put a mark there and the stamp AFTER it
# (`## Correction — 2026-10-16T08:06:29Z`), legacy ones carry a bare date.
# Parser discipline: read the DATE from its own shape, never from a positional
# group — a positional read hands back "Correction" as the timestamp and every
# era comparison built on it silently fails.
STAMP_RE = re.compile(r"(\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}(?:[+-]\d{2}:?\d{2}|Z)?)?)")


def entry_stamp(header: str) -> str:
    """The entry's timestamp, wherever the header puts it."""
    m = STAMP_RE.search(header)
    return m.group(1) if m else ""


def _transcript_note_rows(state_dir: Path) -> list[dict[str, Any]]:
    """All `note` rows from the united stream — active plus archive.

    Rotation cuts the active transcript mid-round, so a file-local read would
    restart at the cut and report every older entry as an orphan. Honest caveat:
    the archive itself rotates (TRANSCRIPT_ARCHIVES = 4), so a round older than
    the surviving archive cannot prove its rows and the leg reports what it can
    see rather than pretending to a full history.
    """
    rows: list[dict[str, Any]] = []
    # Transcript-archive, NOT notes/archive: rotation shifts the active
    # transcript to `transcript-archive/N.jsonl` (manifest.py:56). Reading the
    # notes archive instead yields [] silently, and after any rotation every
    # entry whose journal row rotated reads as an orphan — the leg refuses
    # closes it should allow, which is the same false-refusal class as the
    # round-104 false pass. Live-measured by the critic (seq 1597).
    archive = state_dir / "transcript-archive"
    paths = sorted(archive.glob("*.jsonl")) if archive.is_dir() else []
    active = state_dir / "transcript.jsonl"
    if active.exists():
        paths.append(active)
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                row = __import__("json").loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("kind") == "note":
                rows.append(row)
    return rows


def attribution_floor(state_dir: Path) -> str:
    """When the appender began stamping provenance — the era boundary.

    The earliest `ts` among journal note rows that CARRY a note_sha. Entries
    predating it were written by an appender that had no sha field yet, so their
    provenance is absent by construction, not by tampering. Empty stream means
    no boundary is known and nothing is scoped out (fail closed, the leg then
    treats everything as in-era).
    """
    stamps = [str(r.get("ts") or "") for r in _transcript_note_rows(Path(state_dir))
              if r.get("note_sha")]
    return min(stamps) if stamps else ""


def check_notes(state_dir: Path) -> tuple[bool, str]:
    """Cross-check every notes/<kind>.md entry against the transcript's note rows.

    Returns `(ok, detail)`. Blocking and non-blocking are SEPARATE (critic
    ruling, round 106, decision-round106-rulings): only an entry that COULD have
    been checked and does not match blocks. Report-only, named and counted:
    - an entry predating the attribution floor — its provenance is absent by
      construction (the appender had no sha field yet), so it cannot be judged;
    - an entry whose journal row rotated away (its sha never appears anywhere in
      the surviving stream) — its only sin is rotation, not tampering;
    - a transcript row with no entry in any notes/ file — content that exists
      only in the journal.

    Blocking, refused:
    - an attribution-era entry (at/after the floor) whose (role, note_sha,
      attempt) has NO matching row anywhere in the surviving stream: the
      hand-edit / foreign-write shape this leg exists to catch. Pre-attribution
      entries are never in this class, because nothing could have matched them.

    Fail-open only when nothing can be read at all (no notes dir, no rows): an
    empty tree is not "consistent", it is unprovable, which the caller treats
    as unknown, not as ok.
    """
    state_dir = Path(state_dir)
    notes_dir = state_dir / "notes"
    if not notes_dir.is_dir():
        return True, "no notes directory: nothing to check"

    floor = attribution_floor(state_dir)
    journal_keys = {
        (str(row.get("role") or ""), str(row.get("note_sha") or ""), int(row.get("note_attempt") or 0))
        for row in _transcript_note_rows(state_dir)
    }
    journal_shas = {str(r.get("note_sha") or "") for r in _transcript_note_rows(state_dir)}

    pre_era: list[str] = []
    rotated: list[str] = []
    forged: list[str] = []
    file_keys: set[tuple[str, str, int]] = set()
    for path in sorted(notes_dir.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if path.name == "archive":
            continue
        for block in re.split(r"^(?=##\s)", text, flags=re.MULTILINE):
            if not block.strip():
                continue
            header = block.splitlines()[0].strip() if block.strip() else ""
            match = NOTE_ENTRY_RE.match(header)
            if not match:
                continue
            # A note entry always carries a timestamp; a markdown section head
            # (`## P3 — hygiene`, `## Итоги раунда 75`) can match the shape
            # without one and must never be counted as an unprovable entry —
            # measured live: plan.md's section heads fired the leg on files
            # that are not note logs at all.
            if not STAMP_RE.search(header):
                continue
            ts, role, sha, attempt = match.groups()
            stamp = entry_stamp(header) or ts
            if not sha:
                # No sha at all: in-era this is the unprovable case (blocking),
                # pre-era it is the same construction as everything else.
                if floor and stamp < floor:
                    pre_era.append(f"{path.name}: {stamp} — {role}")
                else:
                    forged.append(f"{path.name}: entry {stamp} — {role} carries no note sha/attempt")
                continue
            key = (role, sha, int(attempt))
            file_keys.add(key)
            if key in journal_keys:
                continue
            # The entry names a sha. Does that sha exist ANYWHERE in the
            # surviving stream under any attempt? Yes -> rotation, report only.
            # No -> nothing in the journal ever knew this body: blocking.
            if sha in journal_shas:
                rotated.append(f"{path.name}: entry {stamp} — {role} (note {sha} attempt {attempt})")
            elif floor and stamp < floor:
                # A pre-attribution entry that happens to carry a sha-ish tail
                # cannot be judged against a stream that had no such field.
                pre_era.append(f"{path.name}: {stamp} — {role}")
            else:
                forged.append(f"{path.name}: entry {stamp} — {role} (note {sha} attempt {attempt}) has no transcript row")

    journal_only = sorted(k for k in journal_keys - file_keys)
    if forged:
        return False, ("note entries that could be checked and do not match — "
                       "hand-edited or foreign-written: " + "; ".join(forged[:5]))
    parts: list[str] = [f"every checkable note entry matches its journal row ({len(file_keys)} checked)"]
    if pre_era:
        parts.append(f"{len(pre_era)} pre-attribution entry(ies) counted as history: "
                     + "; ".join(pre_era[:5]))
    if rotated:
        parts.append(f"{len(rotated)} entry(ies) whose journal row rotated away: "
                     + "; ".join(rotated[:5]))
    if journal_only:
        parts.append("journal rows with no note entry: "
                     + "; ".join(f"{r}/{s} attempt {a}" for (r, s, a) in journal_only[:5]))
    return True, " — ".join(parts)
