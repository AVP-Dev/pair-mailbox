"""Isolation strategy: what the pair agreed to isolate, and whether it fits.

Why this module exists. The backlog position (P1, `.pair/notes/backlog.md:137`) says
a one-sided writer decision about worktrees goes both ways: too much machinery for
a typo fix, or one thousand-line system change in a single PR nobody can review.
So the strategy becomes a DECLARED, CHECKED thing rather than a habit:

- the writer states the strategy in the round plan, under `# Isolation Strategy`;
- the critic's visa covers that plan — the same `plan-ack round N` the plan gate
  already asks for, no new kind of visa to invent;
- the strategy is checked against the FACTUAL size of the change, so a declared
  "small fix" that turns into 400 lines is refused.

The three modes, and what each one means:

    in-place    the whole change is one slice on the round branch (< 50 lines).
                No worktree: the overhead buys nothing at that size.
    worktree    one slice, in one isolated worktree (<= 300 lines), removed
                after its PR is merged.
    decomposed  the change is split into `slices` sequential slices, each
                <= 300 lines, each verified on its own. `worktrees: 0` means the
                slices are sequential on the round branch (§19: one branch per
                round); `worktrees: N` (N == slices) means each slice gets its
                own worktree, which is what a PARALLEL run needs.

The distinction between a slice and a worktree is deliberate: sequential slices
on the round branch need no second directory, and claiming "decomposed into 3
worktrees" for work that never created one would be a decoration. The count the
plan declares is the count that was actually used.

One thought, one place. The matrix lives here, the parser lives here, and the
validation lives here, so the three can never disagree about what "medium" means.

Fail-closed by construction: `parse_strategy` on a plan without the section
returns a refusal, not a default. A missing decision is not a decision.
"""

from __future__ import annotations

import re
from typing import Any

# The plan section this module reads. One spelling, one place: the gate that
# requires the section and the parser that reads it must not drift apart.
STRATEGY_HEADING = re.compile(
    r"^#{1,6}\s*(isolation\s+strategy|стратегия\s+изоляции)\b",
    re.IGNORECASE,
)

MODES = ("in-place", "worktree", "decomposed")

# The declared mode, spelled the way a human writes it. Both the English and
# the Russian spelling are accepted, because the protocol itself is bilingual
# and a plan in either language must satisfy the gate.
_MODE_WORDS = {
    "in-place": "in-place",
    "in place": "in-place",
    "inplace": "in-place",
    "branch": "in-place",
    "ветка": "in-place",
    "worktree": "worktree",
    "work-tree": "worktree",
    "воркtree": "worktree",
    "ворк-три": "worktree",
    "decomposed": "decomposed",
    "decompose": "decomposed",
    "slices": "decomposed",
    "декомпоз": "decomposed",
}

# Thresholds: the changed lines of one slice.
SMALL_MAX = 50
MEDIUM_MAX = 300

# A `key: value` line inside the section.
_FIELD = re.compile(r"^\s*[-*]?\s*([A-Za-z_][A-Za-z0-9_-]*)\s*[:=]\s*(.*?)\s*$")


def _normalise_mode(text: str) -> str | None:
    """Map a human's spelling of the mode onto the canonical one."""
    raw = text.strip().lower().strip("`\"'")
    raw = raw.split()[0] if raw.split() else raw
    return _MODE_WORDS.get(raw)


def parse_strategy(plan_text: str) -> dict[str, Any]:
    """Read the `# Isolation Strategy` section out of a plan.

    Returns a dict: {ok, mode, worktrees, slices, rationale, why}.

    `ok` is False when the section is absent or unreadable — a plan without the
    section has NOT chosen a strategy, and treating that as "in-place" would
    silently make the default a decision nobody made.
    """
    lines = plan_text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if STRATEGY_HEADING.match(line):
            start = index + 1
            break
    if start is None:
        return {"ok": False, "mode": None, "worktrees": 0, "slices": 0,
                "rationale": "", "why": "the plan has no `# Isolation Strategy` section"}

    # The section runs until the next heading of the same or higher level.
    body: list[str] = []
    in_fence = False
    for line in lines[start:]:
        stripped = line.lstrip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            # A fenced block is an EXAMPLE. A plan that shows the format
            # (`mode: worktree`) inside a fence is documenting the shape, not
            # declaring it — reading the example as the declaration would
            # invert the strategy of every plan that documents one.
            continue
        if stripped.startswith("#"):
            break
        body.append(line)

    mode: str | None = None
    worktrees = 0
    slices = 0
    rationale = ""
    slice_names: list[str] = []
    for line in body:
        if not line.strip():
            continue
        match = _FIELD.match(line)
        if not match:
            # Prose is not a declaration. "No worktree is needed here" would
            # otherwise declare a worktree, and "the branch is round-147" would
            # declare `in-place` — both from a sentence that said the opposite.
            # The declaration is the `mode:` field, and only that.
            continue
        key, value = match.group(1).lower(), match.group(2)
        if key == "mode":
            mode = _normalise_mode(value)
        elif key == "worktrees":
            try:
                worktrees = int(value.strip())
            except ValueError:
                worktrees = 0
        elif key == "slices":
            try:
                slices = int(value.strip())
            except ValueError:
                slices = 0
        elif key == "rationale":
            rationale = value.strip()
        elif key == "slice":
            slice_names = [part.strip() for part in value.split(",") if part.strip()]

    if mode is None:
        return {"ok": False, "mode": None, "worktrees": worktrees, "slices": slices,
                "rationale": rationale, "why": "the `# Isolation Strategy` section names no mode "
                                              f"(one of: {', '.join(MODES)})"}
    if mode == "in-place" and (worktrees or slices > 1):
        return {"ok": False, "mode": mode, "worktrees": worktrees, "slices": slices,
                "rationale": rationale, "why": "the strategy says `in-place` and also asks for "
                                              "worktrees or several slices — the two disagree"}
    if mode == "worktree":
        if worktrees < 1:
            worktrees = 1
        if slices and slices != 1:
            return {"ok": False, "mode": mode, "worktrees": worktrees, "slices": slices,
                    "rationale": rationale, "why": "the `worktree` mode is ONE slice in ONE "
                                                  "worktree — use `decomposed` for more"}
    if mode == "decomposed":
        if slices < 2:
            return {"ok": False, "mode": mode, "worktrees": worktrees, "slices": slices,
                    "rationale": rationale, "why": "a decomposed strategy must name at least "
                                                  "2 slices"}
        if worktrees and worktrees != slices:
            return {"ok": False, "mode": mode, "worktrees": worktrees, "slices": slices,
                    "rationale": rationale, "why": "the worktree count must equal the slice count "
                                                  "(one worktree per slice) or be 0 for "
                                                  "sequential slices on the round branch"}

    return {"ok": True, "mode": mode, "worktrees": worktrees, "slices": slices,
            "rationale": rationale, "slice_names": slice_names, "why": ""}


def size_class(changed_lines: int) -> str:
    """The size class of a change: small, medium or large."""
    if changed_lines < 0:
        return "small"
    if changed_lines <= SMALL_MAX:
        return "small"
    if changed_lines <= MEDIUM_MAX:
        return "medium"
    return "large"


def decide(changed_lines: int) -> dict[str, Any]:
    """The matrix: what strategy does this change's size call for?

    Returns {size_class, mode, worktrees, slices, why}. The recommendation is a
    recommendation — the writer may choose otherwise and say why; what is NOT
    optional is the check in `validate`.
    """
    klass = size_class(changed_lines)
    if klass == "small":
        return {"size_class": klass, "mode": "in-place", "worktrees": 0, "slices": 1,
                "why": (f"up to {SMALL_MAX} changed lines: a branch in place, "
                        "no worktree — the overhead buys nothing")}
    if klass == "medium":
        return {"size_class": klass, "mode": "worktree", "worktrees": 1, "slices": 1,
                "why": (f"{SMALL_MAX + 1}..{MEDIUM_MAX} changed lines: one "
                        "isolated worktree, removed after the merge")}
    return {"size_class": klass, "mode": "decomposed", "worktrees": 0, "slices": 2,
            "why": (f"more than {MEDIUM_MAX} changed lines: decompose into 2-3 "
                    f"sequential slices of at most {MEDIUM_MAX} lines, each "
                    "verified on its own")}


def validate(strategy: dict[str, Any], changed_lines: int) -> tuple[bool, str]:
    """Does the DECLARED strategy match the FACTUAL size of the change?

    The thresholds are checked against the measured diff, never against what the
    plan claimed — otherwise the rule is decoration. `in-place` over the small
    threshold is refused: a big change hiding in a small change's clothes. A
    single `worktree` over the medium threshold is refused: a big change
    pretending to be one slice.
    """
    if not strategy.get("ok"):
        return False, (strategy.get("why")
                       or "the strategy was not parsed — the plan must declare one")
    mode = strategy.get("mode")
    klass = size_class(changed_lines)
    if mode == "in-place":
        if klass != "small":
            return False, (f"the plan says `in-place`, but the change touched "
                           f"{changed_lines} lines — over the {SMALL_MAX}-line small "
                           f"threshold it needs a worktree ({klass})")
        return True, ""
    if mode == "worktree":
        if klass == "large":
            return False, (f"the plan says one worktree, but the change touched "
                           f"{changed_lines} lines — over the {MEDIUM_MAX}-line "
                           "threshold it must be decomposed into slices")
        return True, ""
    if mode == "decomposed":
        slices = int(strategy.get("slices") or 0)
        limit = slices * MEDIUM_MAX
        if changed_lines > limit:
            return False, (f"the plan says {slices} slices of at most {MEDIUM_MAX} "
                           f"lines, but the change touched {changed_lines} — "
                           f"the declared slices cannot hold it (limit {limit})")
        return True, ""
    return False, f"unknown isolation mode {mode!r}"


def agreed_strategy(state_dir: Any, round_number: int, writer: str) -> dict[str, Any]:
    """The isolation strategy of THIS round that the critic has signed.

    Three facts must hold, and each one is checked on its own:

    1. the plan file is the plan of this round and carries a parsable
       isolation strategy;
    2. the critic's visa for this round is no older than that plan — the
       stale-verdict principle (`review.is_stale`), so a visa for an earlier
       revision of the plan does not count;
    3. the visa is not the writer's own.

    Returns {ok, why, mode, worktrees, slices, ack_role, ack_at}. A refusal
    names WHICH of the three failed, because "no worktree without agreement"
    without a reason sends the reader looking in the wrong place.
    """
    from datetime import datetime, timezone
    from pathlib import Path

    # `plan_gate` imports this module for the heading pattern, so the import
    # here is late on purpose: a module-level import would close a cycle.
    import plan_gate

    state_dir = Path(state_dir)
    plan_path = state_dir / plan_gate.PLAN_FILE
    try:
        text = plan_path.read_text(encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "why": f".pair/{plan_gate.PLAN_FILE} unreadable: {exc}",
                "mode": None, "worktrees": 0, "slices": 0,
                "ack_role": None, "ack_at": None}

    plan = plan_gate.parse_plan(plan_path, mode="fix")
    if not plan["ok"]:
        return {"ok": False, "why": plan["why"], "mode": None, "worktrees": 0,
                "slices": 0, "ack_role": None, "ack_at": None}
    if plan["round"] != round_number:
        return {"ok": False,
                "why": (f"the plan marks round: {plan['round']}, but the round is now "
                        f"{round_number}: another round's plan agrees nothing"),
                "mode": None, "worktrees": 0, "slices": 0,
                "ack_role": None, "ack_at": None}

    strategy = parse_strategy(text)
    if not strategy["ok"]:
        return {"ok": False, "why": strategy["why"], "mode": None, "worktrees": 0,
                "slices": 0, "ack_role": None, "ack_at": None}

    try:
        mtime = datetime.fromtimestamp(plan_path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        mtime = datetime.now(timezone.utc)
    ack = plan_gate.critic_ack(state_dir, round_number, writer, mtime)
    if not ack.get("ack"):
        return {"ok": False,
                "why": (f"the isolation strategy of round {round_number} is not agreed: "
                        f"{ack.get('why')}"),
                "mode": strategy["mode"], "worktrees": strategy["worktrees"],
                "slices": strategy["slices"],
                "ack_role": None, "ack_at": None}

    return {"ok": True, "why": "", "mode": strategy["mode"],
            "worktrees": strategy["worktrees"], "slices": strategy["slices"],
            "ack_role": ack.get("role"), "ack_at": ack.get("at")}
