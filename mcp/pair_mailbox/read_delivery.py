"""Pure state transitions for acknowledgement of mailbox read batches.

Round 101: `pair_read` used to mark mail read the moment it answered, so a client
that tore the connection mid-call (or an agent that answered nothing) consumed
the letters silently - seq 1440 was read, `_drain`ed and never delivered. The
batch protocol here is the fix: the batch is persisted BEFORE the reply, stays
pending until its exact token is ACKed, and replays serve the SAME letters the
original batch recorded - by seq, never by file position.
"""
from __future__ import annotations

import uuid
import json
from typing import Any

MAX_REPLAY_ATTEMPTS = 3

# One refusal cause for one condition, shared by drain and acknowledge_batch so
# an agent reading it knows it is drift, not two different bugs.
DRIFT_CAUSE = "read cursor changed since the pending batch was issued"


def create_pending_batch(
    state: dict[str, Any], role: str, start: int, cursor: int, seqs: list[int],
) -> str:
    """Persist the batch cursor before its MCP result is written."""
    batch_id = uuid.uuid4().hex
    state.setdefault("pending_reads", {})[role] = {
        "batch_id": batch_id,
        "start": start,
        "cursor": cursor,
        "seqs": list(seqs),
        "attempts": 1,
        "escalated": False,
    }
    return batch_id


def replay_attempt(state: dict[str, Any], role: str) -> tuple[dict[str, Any], int, bool]:
    """Count a replay, journaling escalation once without consuming the batch."""
    pending = (state.get("pending_reads") or {}).get(role)
    if not isinstance(pending, dict):
        raise ValueError("no pending read batch")
    try:
        attempts = int(pending.get("attempts", 1))
    except (TypeError, ValueError):
        attempts = 1
    attempts = min(MAX_REPLAY_ATTEMPTS, max(1, attempts + 1))
    pending["attempts"] = attempts
    escalated_now = attempts >= MAX_REPLAY_ATTEMPTS and not pending.get("escalated")
    if escalated_now:
        pending["escalated"] = True
    return pending, attempts, escalated_now


def acknowledge_batch(
    state: dict[str, Any], role: str, batch_id: str, *, decision: str = "ack",
    reason: str = "", require_escalated: bool = False,
) -> dict[str, Any]:
    """Advance exactly the cursor protected by a live, single-use batch token."""
    if decision not in {"ack", "skip"}:
        raise ValueError("decision must be 'ack' or 'skip'")
    if decision == "skip" and not reason.strip():
        raise ValueError("skip requires a non-empty reason")
    pending = (state.get("pending_reads") or {}).get(role)
    if not isinstance(pending, dict) or pending.get("batch_id") != batch_id:
        raise ValueError("unknown or already acknowledged read batch")
    if require_escalated and not pending.get("escalated"):
        raise ValueError("operator read-ack is allowed only after replay escalation")
    seen = state.setdefault("seen", {})
    if int(seen.get(role, 0)) != int(pending.get("start", -1)):
        raise ValueError(DRIFT_CAUSE)
    seen[role] = int(pending["cursor"])
    del state["pending_reads"][role]
    return {
        "decision": decision,
        "batch_id": batch_id,
        "seqs": list(pending.get("seqs") or []),
        "reason": reason.strip(),
    }


def pending_summary(state: dict[str, Any], role: str) -> dict[str, Any] | None:
    pending = (state.get("pending_reads") or {}).get(role)
    if not isinstance(pending, dict):
        return None
    try:
        attempts = int(pending.get("attempts", 1))
    except (TypeError, ValueError):
        attempts = 1
    return {
        "batch_id": pending.get("batch_id"),
        "seqs": list(pending.get("seqs") or []),
        "attempts": attempts,
        "escalated": bool(pending.get("escalated")),
    }


def _migrate_legacy(state: dict[str, Any], role: str) -> None:
    """Fold a legacy `_read_attempts` counter into its batch, once.

    Batches written before the fold kept attempts in a second underscore key;
    any state still carrying it moves the number into `pending_reads[role]` and
    the key is dropped, so the shared state document carries one field.
    """
    legacy = (state.get("_read_attempts") or {}).pop(role, None)
    state.pop("_read_attempts", None)
    if legacy is None:
        return
    pending = (state.get("pending_reads") or {}).get(role)
    if isinstance(pending, dict):
        try:
            current = int(pending.get("attempts", 1))
        except (TypeError, ValueError):
            current = 1
        try:
            carried = int(legacy)
        except (TypeError, ValueError):
            carried = 1
        pending["attempts"] = max(1, current, carried)


def _replay_files(session: Any, files: list[Any], pending: dict[str, Any],
                  ) -> list[Any]:
    """Resolve the recorded seqs to files; refuse when the set shifted.

    Replay is by seq, never by position: `files[start:cursor]` after an
    out-of-order insert or a removal served DIFFERENT letters under the SAME
    token while acknowledge_batch still advanced to the recorded cursor - a
    silent skip. Refusals name the cause instead of serving shifted mail.
    """
    recorded = [int(s) for s in (pending.get("seqs") or []) if str(s).isdigit()]
    start = int(pending.get("start", 0))
    cursor = int(pending.get("cursor", 0))
    # Anything new inside the recorded range means the slice moved under us.
    in_range = files[start:cursor]
    for path in in_range:
        try:
            msg = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise ValueError("recorded batch file became unreadable; ack or skip it "
                             "via pairctl read-ack") from None
        if msg.get("seq") not in recorded:
            raise ValueError(
                f"new mail arrived inside the pending batch range "
                f"(expected seqs {recorded}); ack or skip via pairctl read-ack")
    by_seq: dict[int, Any] = {}
    for path in files:
        try:
            seq = json.loads(path.read_text(encoding="utf-8")).get("seq")
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(seq, int):
            by_seq[seq] = path
    missing = [s for s in recorded if s not in by_seq]
    if missing:
        raise ValueError(
            f"recorded seq(s) {missing} vanished from the mailbox; "
            f"ack or skip via pairctl read-ack")
    return [by_seq[s] for s in recorded]


class DrainRefused(ValueError):
    """A replay that cannot honestly serve its recorded letters."""


def drain(session: Any, limit: int) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    def apply(state: dict[str, Any]):
        session._register(state)
        _migrate_legacy(state, session.role)
        files = session._inbox_files()
        pending = (state.get("pending_reads") or {}).get(session.role)
        replayed = isinstance(pending, dict)
        escalation = None
        if replayed:
            if int((state.get("seen") or {}).get(session.role, 0)) != int(pending.get("start", -1)):
                raise ValueError(DRIFT_CAUSE)
            pending, attempts, escalated_now = replay_attempt(state, session.role)
            try:
                fresh = _replay_files(session, files, pending)
            except ValueError:
                # The batch cannot be replayed honestly; it stays pending and the
                # operator resolves it - never a positional fallback.
                if escalated_now:
                    escalation = {"batch_id": pending["batch_id"],
                                  "seqs": list(pending.get("seqs") or []),
                                  "attempts": attempts}
                raise
            if escalated_now:
                escalation = {"batch_id": pending["batch_id"],
                              "seqs": list(pending.get("seqs") or []),
                              "attempts": attempts}
        else:
            seen = int(state.get("seen", {}).get(session.role, 0))
            if seen > len(files):
                seen = len(files)
            start = seen
            fresh = files[seen:seen + max(0, int(limit))]
            attempts = 1

        out: list[dict[str, Any]] = []
        seqs: list[int] = []
        consumed = 0
        for path in fresh:
            try:
                msg = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                session.store.append_transcript("message_unreadable", session.role,
                                                path=str(path), error=str(exc))
                break
            consumed += 1
            try:
                import rolekeys as _keys
                env = msg.get("envelope")
                verifier = (_keys.load_verify_key(session.store.root, msg.get("from", ""))
                            if isinstance(env, dict) else None)
                if verifier is not None and not _keys.verify_envelope(
                        verifier, env, msg.get("text", "")):
                    session.store.append_transcript(
                        "bad_signature", session.role, path=str(path), seq=msg.get("seq"))
                    continue
            except Exception:  # noqa: BLE001
                pass
            out.append(msg)
            seq = msg.get("seq")
            if isinstance(seq, int):
                seqs.append(seq)

        if replayed:
            batch = {
                "batch_id": pending["batch_id"],
                "seqs": list(pending.get("seqs") or []),
                "attempts": attempts,
                "replayed": True,
                "escalated": bool(pending.get("escalated")),
            }
        elif consumed and out:
            batch_id = create_pending_batch(
                state, session.role, start, start + consumed, seqs)
            batch = {"batch_id": batch_id, "seqs": seqs, "attempts": 1,
                     "replayed": False, "escalated": False}
        else:
            batch = None
            if consumed:
                # Invalid signatures are rejected, not held as unread poison.
                state.setdefault("seen", {})[session.role] = start + consumed
            elif start != int((state.get("seen") or {}).get(session.role, 0)):
                state.setdefault("seen", {})[session.role] = start
        return out, batch, escalation

    try:
        messages, batch, escalation = session.store.mutate(apply)
    except ValueError as exc:
        # Drain refusals are protocol answers, not crashes: same wording the
        # ack path uses, so one condition reads as one condition.
        raise DrainRefused(str(exc)) from None
    if escalation:
        session.store.append_transcript("pair_read_unacked_escalated", session.role,
                                        **escalation)
    return messages, batch
