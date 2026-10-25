"""The trunk of a project: the branch the round's gates measure against.

Why this module exists. Every close-side gate of the engine compared the
local HEAD with `origin/master` by name (`review.origin_truth`,
`review.hotfix_trailer_check`, `server._pin_round_base`,
`manifest._round_merged`). A project whose trunk is another branch could
therefore merge, deliver and review a round and still never close it: the
gate compared against a branch the round never touched. Round 144 measured
exactly that — the round was merged into `dev` and `pair_advance("done")`
refused with "1 commits ahead of origin/master".

Resolution order (first hit wins):

1. `PAIR_TRUNK` — the environment, for one process (the pilot, a CI job).
2. `<project>/.pair/trunk` — the LOCAL setting of this workspace. It lives in
   the ignored state dir, so a local choice never dirties the tree and never
   travels.
3. `<project>/.pair-trunk` — the COMMITTED policy of the project, for
   everybody who clones it.
4. `master` — the default. A project that sets nothing keeps the behaviour it
   had, byte for byte.

The default is what makes this safe to land: no existing project changes
until it says which branch it wants.

The resolver reads a branch NAME, never a remote, never the network: it is
asked on hot paths (every `pair_state`, every close), and a gate that can
hang on a network call is a gate that hangs.
"""
from __future__ import annotations

import os
from pathlib import Path

#: The branch a project gets when it says nothing.
DEFAULT_TRUNK = "master"

#: The environment variable that overrides everything, for one process.
ENV_VAR = "PAIR_TRUNK"

#: The local, ignored setting of one workspace.
LOCAL_FILE = Path(".pair") / "trunk"

#: The committed policy of the project.
PROJECT_FILE = Path(".pair-trunk")


def _first_line(path: Path) -> str:
    """The first non-empty line of a file, or "" when it cannot be read."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    for line in text.splitlines():
        name = line.strip()
        if name:
            return name
    return ""


def resolve_trunk(project_root: Path | str | None = None) -> str:
    """The trunk branch name of a project.

    `project_root` is the project directory (not the `.pair` state dir): the
    settings live beside the code, the state dir holds the runtime. `None`
    means the current directory.
    """
    root = Path(project_root) if project_root is not None else Path.cwd()
    env = os.environ.get(ENV_VAR, "").strip()
    if env:
        return env
    for rel in (LOCAL_FILE, PROJECT_FILE):
        name = _first_line(root / rel)
        if name:
            return name
    return DEFAULT_TRUNK


def trunk_ref(project_root: Path | str | None = None, remote: str = "origin") -> str:
    """The remote-tracking ref of the project's trunk, e.g. `origin/dev`."""
    return f"{remote}/{resolve_trunk(project_root)}"
