"""Which room is mine? One resolver, so every entry point answers alike.

Round 121's ruling: "the project root owns the state, never the inherited
environment". It was implemented in two places and contradicted in three, and
the cost was never the bugs — it was that the rule had no single place, so the
next entry point would get it wrong the same way, and "which room is mine?"
could only be answered by reading five code paths.

Round 127 measured what a wrong answer costs. From a project subdirectory,
`bin/pair-duo` took a foreign `PAIR_STATE` on its face, wrote a WRITER into
that room and launched the panes bound to it; `bin/pairpilot` created a second
room under the subdirectory and watched THAT one, and the nearer `.pair` wins
every later walk-up, so the second room hides the real one from the next tool
that resolves by walking up.

The rule, unchanged since round 121:

- the project root is authoritative: the nearest `.pair/state.json` found by
  walking up from cwd, bounded by the git root;
- an INHERITED `PAIR_STATE` is honoured only when it names THIS project's own
  room; anything else is refused LOUDLY, with `PAIR_TRUST_ENV=1` as the
  deliberate escape. A quiet repoint is the defect;
- an explicit `--state` on the command line is always trusted: that is the
  operator speaking, not a leftover from another project.

A refusal names both rooms. "I did not use your state" without saying which
one was used is the same silence, one step later.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

TRUST_ENV = "PAIR_TRUST_ENV"


def walk_up_state(cwd: Path | None = None, *, bounded_by_git: bool = True,
                  no_walk_up_above: Path | None = None) -> Path | None:
    """The nearest `.pair` holding a state.json, walking up from cwd.

    Bounded by the git root: without that bound, someone else's `~/work/.pair`
    latches on from a subdirectory. Returns None when nothing is found, so the
    caller decides between "no room" and "a room of my own making" — the
    difference is the litter.

    `no_walk_up_above` stops the walk at a directory even when git would let
    it climb further. It exists for tests, which must not depend on what
    happens to sit above the temp dir on the machine running them.
    """
    here = (cwd or Path.cwd()).resolve()
    top = _git_top(here) if bounded_by_git else None
    ceiling = no_walk_up_above.resolve() if no_walk_up_above else None
    for _ in range(12):
        candidate = here / ".pair"
        if (candidate / "state.json").exists():
            return candidate
        if top is not None and here == top:
            break
        if ceiling is not None and here == ceiling:
            break
        parent = here.parent
        if parent == here:
            break
        here = parent
    return None


def _git_top(start: Path) -> Path | None:
    import subprocess
    try:
        proc = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                              cwd=str(start), capture_output=True, text=True,
                              timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    out = (proc.stdout or "").strip()
    if proc.returncode != 0 or not out:
        return None
    return Path(out)


def resolve_state_dir(
    *,
    explicit: str | None = None,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
    fallback: Path | None = None,
    no_walk_up_above: Path | None = None,
) -> tuple[Path, str]:
    """The room this process serves, and why.

    Returns `(room, reason)`. The reason is what the caller prints when the
    answer is not the obvious one — a refusal is only useful if it names both
    rooms.
    """
    environ = os.environ if env is None else env
    here = (cwd or Path.cwd()).resolve()

    # An explicit --state is the operator speaking: it wins, always.
    if explicit:
        return Path(explicit).expanduser().resolve(), "explicit --state"

    walked = walk_up_state(here, no_walk_up_above=no_walk_up_above)
    inherited = (environ.get("PAIR_STATE") or "").strip()
    want = _resolved(inherited) if inherited else None

    if walked is None:
        # No room underfoot. The INHERITED room is still a candidate: a
        # trusted PAIR_STATE names a room the operator chose, and it is the
        # only room there is. An untrusted one is refused like anywhere else.
        if want is not None and environ.get(TRUST_ENV) == "1":
            return want, "the inherited room, trusted on purpose (PAIR_TRUST_ENV=1)"
        # No room underfoot and no trusted inherited room: the caller's own
        # project gets a fresh one; the fallback (the install dir) is for a
        # project not set up yet.
        target = (fallback or here / ".pair")
        return target.resolve(), "no room found underfoot"

    mine = walked.resolve()
    if not inherited:
        return mine, "the room underfoot"

    if want is None or want == mine:
        return mine, "the inherited room is this project's own"

    if environ.get(TRUST_ENV) == "1":
        return want, "the inherited room, trusted on purpose (PAIR_TRUST_ENV=1)"

    return mine, (f"refused the inherited PAIR_STATE={inherited}: it belongs to "
                  f"another project; using {mine} instead "
                  "(PAIR_TRUST_ENV=1 to keep it)")


def resolve_state_dir_logged(
    *,
    explicit: str | None = None,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
    fallback: Path | None = None,
    log: "Callable[[str], None] | None" = None,
) -> Path:
    """The room, with a refusal announced.

    The shell entry points cannot print a Python reason, so the reason comes
    back to them as a second line; the Python ones get it logged here. Both
    say the same thing: WHICH room was refused and WHICH room is used instead.
    """
    room, reason = resolve_state_dir(explicit=explicit, env=env, cwd=cwd,
                                     fallback=fallback)
    if log is not None and reason.startswith("refused"):
        log(reason)
    return room


def _resolved(raw: str) -> Path | None:
    try:
        return Path(raw).expanduser().resolve()
    except OSError:
        return None
