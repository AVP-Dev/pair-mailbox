"""The merge strategy of a project: how a round's PR reaches the trunk.

Why this exists. The merge strategy was hard-wired: `run_gh_merge` always
passed `--squash --delete-branch`. A project that wants its rounds rebased
or merged as merge commits had no way to ask for it — except by editing the
engine, which is not a setting.

This module is the setting. It follows `trunk.py`'s pattern exactly, because
a project must not grow a second way of reading configuration:

1. `PAIR_MERGE_STRATEGY` — the environment, for one process.
2. `<project>/.pair/merge-strategy` — the LOCAL setting of this workspace.
3. `<project>/.pair-merge-strategy` — the COMMITTED policy of the project.
4. `squash` — the default.

The default is what makes this safe to land: a project that says nothing
keeps the behaviour it had, byte for byte.

An invalid value is REFUSED, with the admissible values named. It is never
silently replaced by the default: a project that asked for `rebase` and got
`squash` would learn it from the trunk's history, where nobody was looking.
"""
from __future__ import annotations

import os
from pathlib import Path

#: The strategy a project gets when it says nothing. The behaviour that
#: existed before the setting existed — byte for byte.
DEFAULT_STRATEGY = "squash"

#: The strategies `gh pr merge` knows, and the flag each one needs.
STRATEGY_FLAGS: dict[str, list[str]] = {
    "squash": ["--squash"],
    "merge": ["--merge"],
    "rebase": ["--rebase"],
}

#: The environment variable that overrides everything, for one process.
ENV_VAR = "PAIR_MERGE_STRATEGY"

#: The local, ignored setting of one workspace.
LOCAL_FILE = Path(".pair") / "merge-strategy"

#: The committed policy of the project.
PROJECT_FILE = Path(".pair-merge-strategy")


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


def valid_strategies() -> list[str]:
    """The admissible strategies, in a stable order."""
    return list(STRATEGY_FLAGS)


def resolve_merge_strategy(project_root: Path | str | None = None) -> str:
    """The merge strategy of a project, or a refusal.

    `project_root` is the project directory (not the `.pair` state dir): the
    settings live beside the code, the state dir holds the runtime. `None`
    means the current directory.

    The resolution order is "first hit wins", the same as
    `trunk.resolve_trunk`:

    - the environment, for one process;
    - the local, ignored file — one workspace's choice;
    - the committed file — everybody's policy;
    - the default.

    Raises `ValueError` when the value found is not a strategy gh knows. The
    message names the admissible values, because a refusal that does not say
    what would have worked sends the reader looking through the source.
    """
    root = Path(project_root) if project_root is not None else Path.cwd()
    env = os.environ.get(ENV_VAR, "").strip()
    if env:
        name = env
    else:
        name = ""
        for rel in (LOCAL_FILE, PROJECT_FILE):
            found = _first_line(root / rel)
            if found:
                name = found
                break
        if not name:
            name = DEFAULT_STRATEGY
    if name not in STRATEGY_FLAGS:
        raise ValueError(
            f"invalid merge strategy {name!r}; valid strategies are: "
            + ", ".join(valid_strategies()))
    return name


def merge_flags(project_root: Path | str | None = None) -> list[str]:
    """The `gh pr merge` flags for the project's strategy.

    `--delete-branch` is always there: §19 requires the round's branch to
    die with the round, whatever strategy brought it to the trunk.
    """
    strategy = resolve_merge_strategy(project_root)
    return list(STRATEGY_FLAGS[strategy]) + ["--delete-branch"]
