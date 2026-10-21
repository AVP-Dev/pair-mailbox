"""Token budgets: what a read is allowed to cost.

Measured on the live room, 2026-10-21: `pair_task list` answered "one task is open"
for 29 784 tokens, `pair_note_index` listed 229 notes' metadata for 8 065, and a
four-note tail sweep cost 17 052. The budget lives here, not inside the tools: the
tools serve it, and one place decides what a token costs.

The estimate over-insures on purpose. `cl100k_base` runs about 4 characters per token
on English and about 2.6 on Russian — measured here, not assumed. A bytes/4 estimate
under-counts Russian by a third, and a budget built on it hands out more tokens than
it promised. The estimate below over-counts both: ASCII at 3.5 characters per token,
everything else at 2. A budget that says 800 tokens then means 800 tokens.
"""
from __future__ import annotations

import re

# Characters per token, counted from the conservative side. Prose runs about 4.0
# characters per token in English and about 2.6 in Russian, but this room's notes
# are denser than prose: measured on the four working notes (2026-10-21), ASCII
# ran at 3.3 and everything else at 1.9. Both numbers here sit below the measured
# ones, and the chunk count below covers what the ratio still misses.
ASCII_CHARS_PER_TOKEN = 3.0
NON_ASCII_CHARS_PER_TOKEN = 1.8
# A word-like chunk: a run of word characters, or a single symbol. The count of
# these follows code, JSON and paths, where the character ratio loses.
_WORD_CHUNK = re.compile(r"\w+|[^\w\s]")
# The margin on top of the larger of the two estimates. Measured against the real
# tokenizer on this room's own notes, the estimate sat 4 to 24 % under the truth
# without it — and a budget that under-counts hands out more tokens than it
# promised, which is the one failure a budget must not have.
SAFETY_MARGIN = 1.1

# The tail a note read returns, in tokens. The line budget that stood here before
# (120 lines) cost 3 166 to 5 122 tokens on the live room's four working notes,
# because a line of Russian prose is not a fixed number of tokens: a note of long
# lines cost four times a note of short ones, and the reader could not tell in
# advance which one it had opened.
NOTE_READ_TOKEN_BUDGET = 800
# A ceiling on the ceiling: a caller may ask for a bigger tail, but not for a tome.
NOTE_READ_TOKEN_MAX = 4000
# The line window a read keeps by default, and the hard cap on it. The line budget
# stays as a SECOND ceiling: the token budget bounds the cost, the line budget
# bounds the shape, and a note of short lines does not return four hundred of them.
NOTE_READ_DEFAULT = 120
NOTE_READ_MAX = 400
# How many lines a note file may hold before it rotates. Measured live: verdict.md
# grew to 630 lines, about 19 thousand tokens per ONE read. Rotation carries the old
# into archive, leaving a pointer in the file: nothing is lost, and the file stops
# growing without bound.
NOTE_LINE_CAP = 600
# How many notes the index lists by default. The full index of 229 notes cost 8 065
# tokens of metadata — 35 tokens a row, for a listing nobody reads to the end.
NOTE_INDEX_DEFAULT = 12
# The facts store: the anchor that must survive a compaction without the 39 thousand
# tokens of prose a compaction keeps alive.
FACTS_TOKEN_BUDGET = 500


def estimate_tokens(text: str) -> int:
    """An upper bound on the tokens `text` costs. Never a lower bound.

    Two estimates, and the larger one wins. The character ratio (ASCII at 3.0
    characters per token, everything else at 1.8) follows prose; the number of
    word-like chunks follows code, JSON and paths. Neither holds alone, and both
    were measured on this room's own notes: the ratio under-counted the dense
    files by a fifth, and the chunk count under-counted Russian prose by half.

    Over-counting costs a little room. Under-counting costs the ceiling: the agent
    hits the context limit where it counted on the budget holding, which is exactly
    the failure a budget exists to prevent.
    """
    if not text:
        return 0
    ascii_chars = sum(1 for ch in text if ch.isascii())
    other = len(text) - ascii_chars
    by_chars = (ascii_chars / ASCII_CHARS_PER_TOKEN
                + other / NON_ASCII_CHARS_PER_TOKEN)
    by_chunks = len(_WORD_CHUNK.findall(text))
    return int(max(by_chars, by_chunks) * SAFETY_MARGIN) + 1


def fit_prefix(text: str, budget: int) -> str:
    """The longest prefix of `text` that fits the token budget.

    Cut by the same arithmetic the estimate uses. The digest used to cut BYTES at
    four per token while the estimate counted characters at 3.5, so the cut line came
    out over the budget and the final cleanup dropped it whole — the task start, the
    one line the digest exists for, gone with it.
    """
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if estimate_tokens(text[:middle]) <= budget:
            low = middle
        else:
            high = middle - 1
    return text[:low]


def split_records(lines: list[str]) -> list[list[str]]:
    """The note's records, each starting at its `## ` heading.

    A note is a series of `## <time> — <role>` blocks. Splitting on them is what
    lets a read cut without handing out half a record: a half record reads as a
    whole one, and the reader acts on it.
    """
    records: list[list[str]] = []
    for line in lines:
        if line.startswith("## ") or not records:
            records.append([line])
        else:
            records[-1].append(line)
    return records


def fit_records_to_budget(records: list[list[str]], budget: int) -> tuple[list[list[str]], int]:
    """The newest records that fit the budget, whole, and what they cost.

    A record is never split. When the newest record alone is bigger than the budget,
    it comes back whole and the caller says the budget did not hold: a fact, not a
    silent truncation. Cutting inside a record would save the budget and cost the
    reader the truth.
    """
    kept: list[list[str]] = []
    used = 0
    for record in reversed(records):
        cost = estimate_tokens("\n".join(record))
        if kept and used + cost > budget:
            break
        kept.insert(0, record)
        used += cost
    if not kept and records:
        kept = [records[-1]]
        used = estimate_tokens("\n".join(kept[0]))
    return kept, used
