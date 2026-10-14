"""Whose pane this is: this project or another.

The question is split from `pane_is_foreign` on purpose, and the split is not
cosmetic. `pane_is_foreign` answers "may we print" and so also asks "is the
pane alive". Here only "is it this project" is asked, because `_register`
writes the pane into the roster, and the roster needs no death verdict: a dead
pane has its own fate, handled by another path.

Mixing the two facts would cost more than it looks. The "ours" check would
answer "no" on a dead pane, and a role with a dead pane would also get a false
"foreign project" in the journal — two different refusals for one cause, to be
untangled from the log.

The module also owns the race at pane birth: right after `split-window -c`
tmux reports the PREVIOUS pane’s cwd, and a naive check would call another pane
ours — or worse, reject the just-born agent.
"""
from __future__ import annotations

import time
from pathlib import Path

# How long to wait for tmux to set the cwd of a newborn pane. Measured:
# the first poll right after `split-window -c` returns the previous pane’s cwd,
# and ~0.3s later the right one. A value with margin: longer waits happen only
# at new-pane registration, not on every ring.
PANE_CWD_SETTLE_SECONDS = 0.6


def _settle_pane_identity(tmux: str, pane: str, ident: dict, project_root) -> dict:
    """Re-asks the pane cwd when the first poll never proved it ours.

    Birth race, measured: right after `split-window -c <dir>` tmux returns
    the PREVIOUS pane’s cwd — wrong, not empty; ~0.3s later the right one.

    So the check repeats once, not "while empty": a wrong path can never be
    waited out of existence — it reads as a confident answer. One repeat covers
    both the empty and the stale cwd, both race shapes. Without it the role
    would reject its own pane in the first milliseconds of its life.
    """
    if _identity_proves_ours(ident, project_root):
        return ident
    time.sleep(PANE_CWD_SETTLE_SECONDS)
    return composer.pane_identity(tmux, pane)


def _identity_proves_ours(ident: dict, project_root) -> bool:
    """Whether the pane snapshot proves it ours."""
    if not ident.get("exists") or not ident.get("alive") or not ident.get("path"):
        return False
    try:
        ours = Path(project_root).resolve()
        theirs = Path(ident["path"]).resolve()
    except OSError:
        return False
    return theirs == ours or ours in theirs.parents


def pane_belongs_to_project(tmux: str, pane: str, project_root) -> tuple:
    """Whether the pane is provably FROM THIS project. `(True, "")` or `(False, reason)`.

    Unlike `pane_is_foreign` there is NO "is the pane alive" question here, and the
    split matters. A dead pane belongs to the project — it is just dead, and
    another path in charge of stale roster entries must drop it. Never mix the
    two facts: the "ours" check must not answer "no", or a role with a dead
    pane would also get a false "foreign project" in the journal.

    Unknown (no pane, no cwd reported) is neither "other" nor "ours":
    a refusal returns, because membership stayed unproven.
    """
    ident = composer.pane_identity(tmux, pane)
    if not ident["exists"]:
        return False, "pane missing"
    # Liveness checks BEFORE membership, the only order where both branches
    # answer different things. A pane can be dead AND ours: membership is then
    # provable, and a refusal would be wrong — exactly why the two checks split.
    # The reverse order would say "no" where "yes, but it is gone" belongs, and
    # stale-roster-entry cleanup would stop firing.
    # firing.
    if not ident["alive"]:
        # A dead pane still belongs to the project: whose it is, is known.
        return _identity_proves_ours(ident, project_root), "pane dead"
    if _identity_proves_ours(ident, project_root):
        return True, ""
    fresh = _settle_pane_identity(tmux, pane, ident, project_root)
    if not fresh["exists"]:
        return False, "pane missing"
    if not fresh["path"]:
        return False, "tmux never reported the pane cwd, membership unproven"
    if _identity_proves_ours(fresh, project_root):
        return True, ""
    try:
        theirs = Path(fresh["path"]).resolve()
    except OSError as exc:
        return False, f"unresolvable path: {exc}"
    return False, f"pane belongs to another project: {theirs}"


# The `composer` import mid-file is not decor and not forgetfulness, but
# a cycle demand: `composer` imports `panes` lazily (inside pane_is_foreign),
# and a top-level direct import would close the circle. So the reason stays
# visible at this spot, not in another file — it is written here.
import composer


def pane_is_foreign(tmux: str, pane: str, project_root) -> tuple:
    """Is the pane provably not ours? Fail-closed: unproven ours means no printing.

    Returns (True, reason) when the pane must not be printed to, (False, "") when
    it may. An empty cwd proves nothing, so it reads as "foreign": quietly
    refusing delivery beats typing text into the wrong place.

    The strictness is deliberate, second only to fail-closed: a pane counts
    only when its cwd equals the project root or sits inside it. A pane launched
    from a SHARED parent like ~/work is rejected, though formally that parent
    is the project’s. Otherwise several projects on this machine would claim
    such a pane as theirs at once, and cross-project injection would return by
    exactly the path closed here.
    """
    ident = composer.pane_identity(tmux, pane)
    if not ident["exists"]:
        return True, "pane missing"
    if not ident["alive"]:
        return True, "pane dead"
    settled = _settle_pane_identity(tmux, pane, ident, project_root)
    if not settled["exists"]:
        return True, "pane missing"
    if not settled["alive"]:
        return True, "pane dead"
    if _identity_proves_ours(settled, project_root):
        return False, ""
    if not settled["path"]:
        return True, "tmux never reported the pane cwd, identity unproven"
    try:
        theirs = Path(settled["path"]).resolve()
    except OSError as exc:
        return True, f"unresolvable pane cwd: {exc}"
    return True, f"pane belongs to another project: {theirs}"


