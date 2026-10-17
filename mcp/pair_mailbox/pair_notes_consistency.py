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


def _transcript_note_rows(state_dir: Path) -> list[dict[str, Any]]:
    """All `note` rows from the united stream — active plus archive.

    Rotation cuts the active transcript mid-round, so a file-local read would
    restart at the cut and report every older entry as an orphan. Honest caveat:
    the archive itself rotates (TRANSCRIPT_ARCHIVES = 4), so a round older than
    the surviving archive cannot prove its rows and the leg reports what it can
    see rather than pretending to a full history.
    """
    rows: list[dict[str, Any]] = []
    paths = sorted((state_dir / "notes" / "archive").glob("*.jsonl")) if (state_dir / "notes" / "archive").is_dir() else []
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


def check_notes(state_dir: Path) -> tuple[bool, str]:
    """Cross-check every notes/<kind>.md entry against the transcript's note rows.

    Returns `(ok, detail)`. Three classes, each named:
    - an entry whose (role, note_sha, attempt) has NO matching transcript row —
      the hand-edit case: written into the file without the journal's knowledge,
      which is how a foreign-agent write into a kind file hides;
    - a transcript row with no entry in any notes/ file — content that exists
      only in the journal;
    - an entry with no `note <sha> attempt <n>` tail at all, written by a tool
      that is not pair_note: unprovable provenance, so it cannot be matched
      either way.

    Refuses on any of the three. Fail-open ONLY when nothing can be read at all
    (no notes dir, no transcript) — a silent pass is the one result that must
    never happen, and an empty tree is not "consistent", it is unprovable, which
    the caller treats as unknown, not as ok.
    """
    state_dir = Path(state_dir)
    notes_dir = state_dir / "notes"
    if not notes_dir.is_dir():
        return True, "no notes directory: nothing to check"

    journal_keys = {
        (str(row.get("role") or ""), str(row.get("note_sha") or ""), int(row.get("note_attempt") or 0))
        for row in _transcript_note_rows(state_dir)
    }

    orphans: list[str] = []
    unprovable: list[str] = []
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
            ts, role, sha, attempt = match.groups()
            if not sha:
                unprovable.append(f"{path.name}: entry {ts} — {role} carries no note sha/attempt")
                continue
            key = (role, sha, int(attempt))
            file_keys.add(key)
            if key not in journal_keys:
                orphans.append(f"{path.name}: entry {ts} — {role} (note {sha} attempt {attempt}) has no transcript row")

    journal_only = sorted(k for k in journal_keys - file_keys)
    if orphans or unprovable or journal_only:
        parts: list[str] = []
        if orphans:
            parts.append("note entries with no journal row (hand-edited or foreign-written): "
                         + "; ".join(orphans[:5]))
        if unprovable:
            parts.append("note entries with no provable provenance: "
                         + "; ".join(unprovable[:5]))
        if journal_only:
            parts.append("journal rows with no note entry: "
                         + "; ".join(f"{r}/{s} attempt {a}" for (r, s, a) in journal_only[:5]))
        return False, " — ".join(parts)
    return True, f"every note entry matches its journal row ({len(file_keys)} checked)"


def tamper_probe(kind: str, body: str, stamp: str, role: str) -> tuple[bool, str]:
    """The leg's own red-proof shape, so the probe and the production check agree.

    Builds an entry in the header format WITHOUT a matching journal row — the
    exact shape of a note hand-written into a kind file. A caller with a state
    dir containing this entry and no such row must refuse naming it.
    """
    return False, (f"{kind}.md: entry {stamp} — {role} carries the entry format but no "
                   f"journal row exists for it — this is the shape the leg refuses")
