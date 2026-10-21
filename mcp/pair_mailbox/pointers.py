"""The pointers this system types into a neighbour's composer, and its own leftovers.

A ring (the doorbell) and a mailbox pointer are short lines typed into another
agent's input box. The neighbour may never accept them: the Enter is lost and
the text stays in the composer. The composer is then not empty, so every later
delivery into that pane is refused - the pane stays wedged until a human clears
it by hand. Measured live: 7 `deliver_pointer_busy` and 10 `doorbell_refused`
rows, every one of them naming the leftover pointer in its reason.

So the sender cleans up after itself, and only after itself: a tail that is
PROVABLY ours - one of the two shapes below, carrying our own role name, and
nothing else in the composer - is erased. The human's typing is never touched.
"""
from __future__ import annotations

import re

from composer import BOX_GLYPH, capture, composer_line, erase, is_composer_empty

# The two shapes this system types into a neighbour's composer. Both carry the
# SENDER's role name: that is what makes a leftover provably ours.
RING_SHAPE = r"pair: [a-z]+ from {role}\. Read pair_read\.( \(.*\))?"
MAILBOX_SHAPE = r"\[pair\] {role}: new message in the mailbox \(seq \d+\), read pair_read"
# The supervisor types into a neighbour's composer too, and its leftovers must
# be recognisable as ITS own — a peer only ever cleans up pointers carrying the
# peer's own role name. A report wake points at the state, not at the mailbox:
# there is no letter behind it, so a doorbell saying "read pair_read" would
# send the peer looking for one that does not exist.
PILOT_WAKE_SHAPE = r"pair: [a-z]+ from pairpilot\. See pair_state\.( \(.*\))?"


def ring_prompt(role: str, kind: str = "message", detail: str = "",
                where: str = "Read pair_read.") -> str:
    """The doorbell line: a cause, never the letter.

    `detail` is NOT a place for the message text. The human directive: the
    terminal is a display, not a bus between agents. Trimmed text here would be
    a "bus in parts": past one line it never fits anyway, reading as a stub no
    cause follows from.

    `where` says where the cause is to be read: the mailbox for a letter, the
    state for a silence. The supervisor's report doorbell points at the state,
    because there is no letter behind it and a pointer to an empty mailbox
    sends the peer looking for one.
    """
    prompt = f"pair: {kind} from {role}. {where}"
    if detail:
        prompt += f" ({detail})"
    return prompt


def mailbox_pointer(role: str, seq: int) -> str:
    """The pointer to one letter, carrying its seq so a reader can date it."""
    return f"[pair] {role}: new message in the mailbox (seq {seq}), read pair_read"


def own_pointer_length(line: str | None, role: str) -> int:
    """How many characters of a composer line are our own leftover pointer.

    Zero when the composer holds anything else - the human's typing, another
    agent's text, or nothing at all. A line that ends with our pointer but
    holds something before it is not ours to erase.
    """
    if not line:
        return 0
    cleaned = re.sub(f"[{BOX_GLYPH}|]", "", line).strip()
    candidates = [shape.format(role=re.escape(role)) + r"$"
                  for shape in (RING_SHAPE, MAILBOX_SHAPE)]
    candidates.append(PILOT_WAKE_SHAPE + r"$")
    for pattern in candidates:
        match = re.search(pattern, cleaned)
        if not match:
            continue
        head = cleaned[: match.start()].strip()
        if head and not is_composer_empty(head):
            # Something else shares the composer: ours is not the only text
            # there, and erasing by count would eat the neighbour's or the
            # human's words.
            return 0
        return len(match.group(0))
    return 0


def clear_own_pointer(tmux: str, pane: str, role: str) -> int:
    """Erase our own leftover pointer from a neighbour's composer.

    Returns the number of characters erased - zero when there was nothing of
    ours there. The caller re-reads the pane afterwards: an erase that did not
    take must not be reported as a delivery.
    """
    try:
        line = composer_line(capture(tmux, pane))
    except Exception:  # noqa: BLE001 - a pane we cannot read holds nothing of ours
        return 0
    length = own_pointer_length(line, role)
    if not length:
        return 0
    try:
        erase(tmux, pane, length)
    except Exception:  # noqa: BLE001 - the refusal path reports what happened
        return 0
    return length
