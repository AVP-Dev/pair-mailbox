"""A state fingerprint for the `pair_state` cache.

A full `pair_state` answer costs ~939 tokens, while agents call it several times a
round with state usually unchanged between calls. The cache answers with a short
stub — but only when the fingerprint truly moved.

The module moved out of `server.py` because it depends only on what the answer
reads, not how the answer assembles. `server.py` imports it for the same reason as
`wake.py` and `review.py`: showing it stays the single place where the session
lives.

The dependency runs the same way as the other modules: `server.py` depends on this
file, never the reverse.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Callable

## Fields that move on every state call alone, hence never dropping the cache:
## their absence from the fingerprint loses no information about what the agent
## can see — or the cache would never fire.
VOLATILE_STATE_KEYS = ("updated_at", "roles")
VOLATILE_ROLE_KEYS = ("last_seen",)

CACHE_DISABLED_ENV = "PAIR_DISABLE_STATE_CACHE"
UNCHANGED_NOTE = (
    "State unchanged since the last call. Read pair_digest for orientation."
)


def cache_disabled(environ: dict[str, str] | None = None) -> bool:
    """The cache switch.

    Promised by a comment, wired here: tests needing a live answer per call set the
    variable, or the closing-fact check would read the stub passing, checking
    nothing.
    """
    env = environ if environ is not None else __import__("os").environ
    return bool(env.get(CACHE_DISABLED_ENV))


def fingerprint(
    state: dict[str, Any],
    *,
    obligation_names: list[str] | None = None,
    inbox_names: list[str] | None = None,
    peer_inbox_names: dict[str, list[str]] | None = None,
    git_snapshot: Callable[[], Any] | None = None,
    alive: dict[str, bool | None] | None = None,
) -> str:
    """A short fingerprint of all the full `pair_state` answer reads.

    Fields never list by hand: the answer follows evidence, human_ok, verdicts,
    patches, roles, seen plus the obligation files — living on disk, never in
    `state.json`. A miss on any returned a stub with no `obligations` and no
    `evidence_this_round` — exactly the fields the agent called `pair_state` for.

    Only `updated_at` and `last_seen` exclude: they move on every registration
    beside reading, carrying the agent nothing beyond the rest of state.

    Obligation and inbox file names pass as lists, never read here: the module must
    never know where they lie — the session owns that. Same with `alive`:
    `peers.alive` reads a live `os.kill(pid, 0)`, not the state number — the
    process dies outside, state never moves, and with no this snapshot the stub
    would lie that a dead neighbour listens. The session snapshots liveness itself,
    passing it ready — then the module never knows processes at all.

    `git_snapshot` calls when verdicts stand OR the round needs delivery: both
    `_done_blockers` (verdict stale) and the delivery floor read git, and the
    fingerprint must see either. Delivery needs it in mode=fix with no verdicts
    yet — exactly the working round where the writer pushes and the next
    `pair_state` must not answer `unchanged:true` over a stale blocker (A7).
    In mode=audit delivery never applies, so verdicts alone gate the call. The
    caller decides itself whether the snapshot stands ready, passing it ready —
    then the module never knows git at all.
    """
    roles = {
        name: {k: v for k, v in info.items() if k not in VOLATILE_ROLE_KEYS}
        for name, info in (state.get("roles") or {}).items()
        if isinstance(info, dict)
    }
    git_fp = None
    needs_delivery = state.get("mode", "fix") != "audit"
    if (state.get("verdicts") or needs_delivery) and git_snapshot is not None:
        git_fp = git_snapshot()
    cache_key = {
        "state": {k: v for k, v in state.items() if k not in VOLATILE_STATE_KEYS},
        "roles": roles,
        "obligations": sorted(obligation_names or []),
        "inbox": list(inbox_names or []),
        "peer_inbox": {k: sorted(v) for k, v in (peer_inbox_names or {}).items()},
        "git": git_fp,
        "alive": dict(sorted((alive or {}).items())),
    }
    return hashlib.sha256(json.dumps(cache_key, sort_keys=True).encode()).hexdigest()[:12]


def unchanged_answer(state_hash: str) -> dict[str, Any]:
    "A short answer for unchanged state."
    return {"unchanged": True, "hash": state_hash, "note": UNCHANGED_NOTE}