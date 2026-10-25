"""The §20 branch-class table: one copy, shared by the checker and the janitor.

Both consumers classify remote branch names. If each carried its own table the
two would drift, and the drift would let one delete a branch the other called
legal — a classification disagreement that resolves in the wrong direction
because the janitor is the one with `git push --delete`.

Consumers: `scripts/branch-class-check.py` (the passive gate, CI) and
`mcp/pair_mailbox/branch_audit.py` (the janitor, `pairctl branch audit`).
"""
from __future__ import annotations

import re

# §20 class table. Order does not matter: patterns are disjoint by construction.
TRUNK = re.compile(r"^master$")
STRATEGIC = re.compile(r"^(release|support)/.+$")
EPHEMERAL = re.compile(r"^(round|epic|feature|fix|hotfix|chore)[-/].+$")
PERSONAL = re.compile(r"^users/[^/]+/.+$")

# §20 rule 4: accumulators are an antipattern, named as one.
ACCUMULATORS = ("dev", "develop", "integration", "staging", "main", "trunk")


def classify(ref: str, trunk: str = "master") -> str:
    """The §20 class of a remote branch ref, or `anomaly`.

    `trunk` is the project's own trunk branch name: a project whose trunk is
    `dev` must have `dev` classified as the trunk, not as an accumulator
    antipattern. The default keeps `master` the trunk for a project that has
    not said otherwise.

    `master` stays in the trunk class even when the project names another
    trunk: the class is the one no robot may ever delete, and a project that
    still carries `master` is safer with it protected than with it reported
    as an anomaly.
    """
    if ref == trunk or TRUNK.match(ref):
        return "trunk"
    if STRATEGIC.match(ref):
        return "strategic"
    if EPHEMERAL.match(ref):
        return "ephemeral"
    if PERSONAL.match(ref):
        return "personal"
    return "anomaly"
