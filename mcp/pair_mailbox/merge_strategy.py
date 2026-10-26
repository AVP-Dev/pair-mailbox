"""The merge strategy of a project: how a round's PR reaches the trunk.

Why this exists. The merge strategy was hard-wired: `run_gh_merge` always
passed `--squash --delete-branch`. A project that wants its rounds rebased
or merged as merge commits had no way to ask for it — except by editing the
engine, which is not a setting.

This module owns the WHAT (which strategies exist, what flag each one
needs); `project_settings` owns the WHERE (the order the project's voice is
heard in). The split is the one the project uses for every setting: the
order is the trunk's order, and there is exactly one place that knows it.

An invalid value is REFUSED, with the admissible values named. It is never
silently replaced by the default: a project that asked for `rebase` and got
`squash` would learn it from the trunk's history, where nobody was looking.
"""
from __future__ import annotations

from pathlib import Path

import project_settings

#: The strategy a project gets when it says nothing. The behaviour that
#: existed before the setting existed — byte for byte.
DEFAULT_STRATEGY = "squash"

#: The strategies `gh pr merge` knows, and the flag each one needs.
STRATEGY_FLAGS: dict[str, list[str]] = {
    "squash": ["--squash"],
    "merge": ["--merge"],
    "rebase": ["--rebase"],
}

#: The name of the setting, as `project_settings` knows it.
SETTING = "merge_strategy"


def valid_strategies() -> list[str]:
    """The admissible strategies, in a stable order."""
    return list(STRATEGY_FLAGS)


def resolve_merge_strategy(project_root: Path | str | None = None) -> str:
    """The merge strategy of a project, or a refusal.

    The project's voice is heard in `project_settings`' order — the
    environment, the local ignored file, the committed project file, the
    default — and the default is `squash`.

    Raises `ValueError` when the value found is not a strategy gh knows. The
    message names the admissible values, because a refusal that does not say
    what would have worked sends the reader looking through the source.
    """
    name = project_settings.resolve(
        project_root, SETTING, DEFAULT_STRATEGY).strip()
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
