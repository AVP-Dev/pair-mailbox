"""Digest: the pair-state slice under a hard token ceiling.

The module moved out of `server.py` by ownership, not for size: the digest is
its own duty — fitting pair state into a set budget — and it is checked by its
own tests, never raising the server.

The dependency direction matches `statecache`, `liveness`, `wake` and `review`:
`server.py` depends on this file, not the reverse. The token estimate itself moved
OUT to `token_budget.py` (round 135), because the protocol needs it too, not only the
digest: the note read and the facts store budget in tokens as well, and a room with
two estimators has two truths about what a token costs. The ceiling counts on the
text we assemble ourselves, and the estimate must be the same function on the same
text.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from token_budget import estimate_tokens as _estimate_tokens  # noqa: E402 - needs sys.path above
from token_budget import fit_prefix as _fit_prefix  # noqa: E402 - needs sys.path above

# The journal-tail read stays in `server`: both `pairctl log` and the digest
# use it. The import stays lazy for the same reason as in `server` itself:
# `server` imports this module at top level, and a direct import would cycle.
def _transcript_tail(path, limit):
    from server import _transcript_tail as impl

    return impl(path, limit)
def _pending_summary(state, role):
    import read_delivery as impl

    return impl.pending_summary(state, role)


# The digest ceiling. The replaced bundle measured: `pair_state` 939 +
# `pair_task list` 1051 + `pair_transcript` 1734 = 3724 tokens per orientation.
# The digest must cost visibly less, or it is useless: three calls read cheaper
# than figuring out the twelfth call in the same round.
DIGEST_TOKEN_BUDGET = 500
# How many journal records the last-verdict search walks. The journal already
# rotates in a window, but even it must never parse whole on every digest call.
# digest call.
TRANSCRIPT_SCAN_LIMIT = 400
DIGEST_MARKER = "[truncated: did not fit, full state — pair_state]"


def _digest_marker_reserve() -> int:
    """How many tokens to keep for the truncation mark.

    Counted FROM THE MARK ITSELF, never set as a round number. First it was wrong:
    the reserve stood at 20, the mark ran 22, and on exactly fitted text the digest
    gave 501 tokens at a 500 ceiling — either breaking the ceiling or scrubbing the
    line it assembled for. A reserve from a constant desyncs from the mark on its
    very first change.
    """
    return estimate_tokens(DIGEST_MARKER) + 4
# A minimum below which the line never lands. A one-token stub carries no
# meaning while taking room the next line needs.
DIGEST_MIN_LINE = 8


def _render_digest(lines: list[str]) -> str:
    """The assembled digest exactly as it reaches the reader.

    A separate function, because the ceiling checks THIS text. Counting size by
    lines and serving glued text are two different numbers, and newlines fitted
    between them — the digest once gave 502 tokens at a 500 ceiling over them.
    """
    return "\n".join(lines) + "\n"

def estimate_tokens(text: str) -> int:
    """A rough text size estimate in tokens.

    The arithmetic moved to `token_budget.py` (round 135) because the protocol needs it
    too, not only the digest: the note read and the facts store budget in tokens,
    and a room with two estimators has two truths about what a token costs. The
    old form divided BYTES by four, which under-counted Russian by a third — 2.6
    characters per token, measured — and the digest's 500-token ceiling was a
    500-token ceiling in English and a 650-token one in Russian. The shared
    estimate over-counts both, so a ceiling holds whatever the room writes in.
    """
    return _estimate_tokens(text)


def _digest_line(text: str, budget: int) -> str:
    """A budget-cut line with an honest truncation mark.

    A cut line with no mark is quietly wrong data. The mark itself may not fit,
    and then a single ellipsis marker stays.
    """
    if estimate_tokens(text) <= budget:
        return text
    if budget <= 4:
        return "…"
    return _fit_prefix(text, max(budget - 1, 0)).rstrip() + "…"


def _facts_lines(store: "Any") -> list[str]:
    """The stored facts, as short lines for the digest.

    The facts are the answer a compaction summary cannot give: what the round stands
    on, in keys, in a few hundred tokens. The digest carries them before the sections
    it can re-read at will.
    """
    import facts as facts_store

    stored = facts_store.read_facts(store)
    return [f"{item.get('key')}: {_digest_line(str(item.get('value')), 60)}"
            for item in stored[:8]]


def _note_tail(store: "Any", slug: str, lines: int = 4) -> list[str]:
    """A note tail: the latest matters, not all history ever stored.

    Notes write append-only, and for a verdict that means exactly what is needed:
    the decision sits at the end. The start states the previous round, and
    in the digest it only steals room from what is decided.
    """
    path = store.notes_root / f"{slug}.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    return [l for l in text.splitlines() if l.strip()][-lines:]


class _TranscriptReader:
    """Read the journal tail without parsing the whole file.

    A separate class, not a function with a line count, because the need is a
    directed search: "the last verdict note". Parsing the whole journal on every
    digest call would return exactly the cost the digest promises never to pay.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def last_note_of_kind(self, prefix: str) -> dict | None:
        """The last note whose slug starts with `prefix`, or None.

        The order source is the journal itself, not file mtime. `notes/` holds
        ninety-two notes with time-unordered names: mtime sorting would guess,
        because a file may open without changing. The journal order is factual:
        the `pair_note` record carries slug, time and author, written when the
        note is created.
        """
        if not self.path.exists():
            # No journal yet — no verdicts, and that is no breakage. Reading the missing
            # file used to drop the whole digest, so the "what was decided" question cost
            # an error instead of an answer.
            return None
        for entry in reversed(_transcript_tail(self.path, TRANSCRIPT_SCAN_LIMIT)):
            if entry.get("_damaged") or entry.get("kind") != "note":
                continue
            slug = str(entry.get("note_kind") or "")
            if slug.startswith(prefix):
                return {"slug": slug, "ts": entry.get("ts"), "role": entry.get("role")}
        return None


def _last_commit(store: "Any") -> str:
    """The last commit in one line.

    From git, not the journal: the journal never knows what committed, and the
    agent reading the digest must see the actual tree state. When git is
    unreachable, that is said, not silenced.
    """
    root = store.project_root
    try:
        out = subprocess.run(
            ["git", "log", "-1", "--format=%h %s"],
            cwd=str(root), capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return "LAST COMMIT: (git unreachable)"
    if out.returncode != 0 or not out.stdout.strip():
        return "LAST COMMIT: (no commits)"
    return f"LAST COMMIT: {out.stdout.strip()}"


def _open_obligations(store: "Any") -> list[dict[str, Any]]:
    """Open obligations, with no lock on the directory."""
    root = store.obligations_root
    if not root.is_dir():
        return []
    out = []
    for path in sorted(root.glob("*.json")):
        try:
            out.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return out


def _subject_line(state: dict[str, Any]) -> str:
    """The round's subject — one rule, four surfaces (task_subject.subject_line)."""
    from task_subject import subject_line as impl

    return impl(state)


def _task_is_stale(state: dict[str, Any]) -> bool:
    """state["task"] set and naming no current round: it outlived its round."""
    from task_subject import scalar_names_round as impl

    raw = str(state.get("task") or "").strip()
    return bool(raw) and not impl(state)


def build_digest(store: "Any", role: str = "", budget: int = DIGEST_TOKEN_BUDGET) -> str:
    """The pair-state slice that fits a hard token budget.

    Why it exists, measured: pair orientation costs 3724 tokens (`pair_state` 939
    + `pair_task list` 1051 + `pair_transcript` 1734). The digest must run visibly
    cheaper, or it is useless — three calls read cheaper than figuring out the
    twelfth call in the same round.

    Text, not JSON: the agent reads the answer with eyes and pays twice for JSON —
    for keys and for values. Short text reads once.

    SECTION ORDER DECIDES EVERYTHING. It follows the question price: what the work
    goes wrong without stands first and never cuts; what can be read point-wise
    stands last and cuts first. Shuffled sections would cost the digest more at the
    same contents — so the order is the decision substance, not decoration.

    Nothing is invented: every section either reads from state or names itself
    missing. The verdict quotes, never retells: retelling it in own words is how
    the unread gets lied about.
    """
    state = store.read()
    lines: list[str] = []
    cut = False  # something missed the fit and was cut

    def budget_left() -> int:
        """How many tokens the text gathered so far may still spend."""
        return budget - estimate_tokens("\n".join(lines))

    def take(text: str, floor: int = 0) -> bool:
        """Add the line when the budget fits, else the cut one.

        The truncation-mark reserve subtracts HERE, never scrubbed at the end.
        First it went wrong: the line cut exactly to the room left, but the final
        cleanup saw "room gone" and dropped it whole — with the task start the
        digest is read for. An upfront reserve makes the cleanup needless.
        """
        """Add the line when the budget fits, else the cut one.

        Returns whether the line was cut or never fitted at all: both cases want
        the end mark, and telling them apart is needless — losing either is what
        matters.

        The truncation-mark reserve subtracts HERE, never scrubbed at the end.
        First it went wrong: the line cut exactly to the room left, but the final
        cleanup saw "room gone" and dropped it whole — with the task start the
        digest is read for. An upfront reserve makes the cleanup needless.
        """
        room = budget_left() - _digest_marker_reserve() - floor
        if room < DIGEST_MIN_LINE:
            # The line never fitted — neither whole nor as a readable stub. A one-token
            # tail is useless and, worse, ate room: the "mode: fix · phase…" line cost one
            # token and squeezed out the task start the digest is read for.
            # Better to skip it and say what missed the fit.
            return True
        cut = _digest_line(text, room)
        lines.append(cut)
        return cut != text

    # 1. What the work is wrong without. Nothing is cut.
    # subject_line, not the raw state["task"] scalar: round 103's digest read
    # "Round 98: …" as this round's subject while round 103 worked, because the
    # guard covered only the open-task branch and nothing was open by then.
    subject = _subject_line(state)
    task = subject or "(task not set)"
    if _task_is_stale(state) and "stale" not in task:
        task = f"{task} (state.task is stale — subject from open tasks)"
    cut |= take("ROUND", floor=DIGEST_MIN_LINE)
    cut |= take(f"task: {task}")
    cut |= take(f"mode: {state.get('mode', '?')} · phase: {state.get('phase', '?')} · "
         f"round {state.get('round', 0)}")
    cut |= take(f"writes: {state.get('writer') or '—'}"
         + (f" (you)" if role and state.get("writer") == role else ""))

    # 1b. The facts anchor: what the round stands on, in keys a reset cannot lose.
    # The prose it replaces — the compaction summary — cost 39 513 to 49 131 tokens
    # and could not be queried. These lines are cheaper than the sections below
    # them and answer the same question, so they come first.
    for line in _facts_lines(store):
        cut |= take(f"  {line}")

    # 2. Open tasks. Closed ones carry no meaning for the next step: eight closed
    #    tasks cost ~800 tokens and said nothing new.
    open_tasks = [t for t in (state.get("tasks") or []) if t.get("status") == "open"]
    cut |= take("")
    cut |= take(f"OPEN TASKS: {len(open_tasks)}")
    if not open_tasks:
        cut |= take("(no open tasks — check whether to add one)")
    for item in open_tasks[:4]:
        cut |= take(f"#{item.get('id')} {_digest_line(str(item.get('text', '')), 60)}")
    if len(open_tasks) > 4:
        cut |= take(f"…and {len(open_tasks) - 4}: `pair_task list`")

    # 2b. [R101] A read batch returned but never ACKed. It decides the next read
    #     (replay vs fresh), so it stands with the open tasks, ahead of everything
    #     that merely informs.
    if role:
        pending = _pending_summary(state, role)
        if pending:
            cut |= take("")
            cut |= take(
                f"PENDING READ: seqs {pending['seqs']} · attempts {pending['attempts']}"
                + (" · ESCALATED: pairctl read-ack" if pending["escalated"] else ""))
            poison = pending.get("poison") or {}
            if poison:
                # A wedged batch that only verbose state could see is the
                # silent-wedge class this round exists to kill: the digest
                # names the path, the cause and the cure.
                cut |= take(f"  POISONED: {poison.get('path') or '(file unnamed)'}")
                cut |= take(f"  cause: {poison.get('cause', '')}", floor=40)
                cut |= take(f"  cure: {poison.get('cure', '')}", floor=40)

    # 3. Obligations. Their absence is a fact too, not emptiness.
    obligations = _open_obligations(store)
    cut |= take("")
    cut |= take(f"OBLIGATIONS: {len(obligations)}")
    if not obligations:
        cut |= take("(nobody waits on anyone)")
    for ob in obligations[:3]:
        cut |= take(f"{ob.get('from', '?')} waits {ob.get('to', '?')}")

    # 4. Last commit: from git, not the journal.
    cut |= take("")
    cut |= take(_last_commit(store), floor=60)

    # 5. Last verdict: a tail quote, order from the journal.
    verdict = _TranscriptReader(store.transcript_path).last_note_of_kind("verdict")
    cut |= take("")
    cut |= take("LAST VERDICT", floor=DIGEST_MIN_LINE)
    if not verdict:
        cut |= take("(no verdicts yet)")
    else:
        cut |= take(f"{verdict.get('role', '?')} · {verdict.get('slug', '?')} · "
             f"`note_read`", floor=70)
        for line in _note_tail(store, verdict["slug"]):
            cut |= take(f"  {_digest_line(line, 120)}")

    # What missed the fit — say it straight. A quietly shortened digest beats a
    # long one downwards: the agent decides no verdict ever was.
    #
    # The ceiling here is no eyeball: it checks on the assembled text — while it
    # fits, the `take` reserve holds, but newlines and the mark itself never entered
    # the estimate, and the digest gave 502 tokens at a 500 ceiling. A promise never
    # checked on what is served is no promise.
    dropped = False
    if dropped or cut:
        marker = DIGEST_MARKER if estimate_tokens(DIGEST_MARKER) < budget else "…"
        # Room for the mark reserves upfront, and the render checks together with it.
        # Otherwise the second pass scrubs it back — and the digest ends without a
        # single word about the lost half of its content.
        while len(lines) > 1 and estimate_tokens(_render_digest(lines + [marker])) > budget:
            lines.pop()
        lines.append(marker)
    return _render_digest(lines)