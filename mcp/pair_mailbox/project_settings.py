"""Project-level settings: one way to read them, wherever they live.

Why this module exists. Round 148 adds two settings that a project owns —
`merge_strategy` (how a PR is finalized) and the list of shared worktree
caches. Both need a place to live, and the project already had one precedent
for such a place: `trunk.py` resolves the trunk branch from, in order, the
environment, a local ignored file, a committed project file, and a default.

Without a shared module, each setting invents its own resolution order, and
the project ends up with three ways to read a setting — three places to look
when a round refuses for the "wrong" reason, and three chances for two of
them to disagree. This module is the one place that knows the order; the
settings themselves only say WHAT they are.

The order is the same as the trunk's, and for the same reasons:

1. **The environment** — for one process (the pilot, a CI job). A process that
   was told explicitly must not be overruled by a file.
2. **`<project>/.pair/<name>`** — the LOCAL setting of one workspace. It lives
   in the ignored state dir, so a local choice never dirties the tree and
   never travels to anybody else.
3. **`<project>/.<name>`** — the COMMITTED policy of the project, for
   everybody who clones it.
4. **The default** — what a project gets when it says nothing.

First hit wins. The default is what makes this safe to land: no existing
project changes until it says otherwise.

The values are read as text and handed back as text. A setting that means
something more than a word (the shared-cache list) is parsed by the module
that owns it, not here: this module answers "what did the project say", and
each owner answers "is that a thing I can use".
"""
from __future__ import annotations

import os
from pathlib import Path

#: The environment variable that overrides everything, for one process.
ENV_VAR = "PAIR_SETTING"

#: The environment variable of each known setting.
#:
#: The names are spelled out rather than derived, because the derivation
#: (`PAIR_SETTING_<NAME>`) invents a second naming convention next to the
#: trunk's `PAIR_TRUNK`. A project that already reads `PAIR_TRUNK` to find
#: the trunk must not have to learn a different rule for the next setting.
#: The prefix keeps the project's settings out of the general environment
#: namespace, where a generic name like `MERGE_STRATEGY` would collide with
#: somebody else's tool.
ENV_NAMES = {
    "merge-strategy": "PAIR_MERGE_STRATEGY",
    "worktree-share": "PAIR_WORKTREE_SHARE",
}


def env_var(name: str) -> str:
    """The environment variable that carries the setting `name`.

    A known setting has its own spelled-out name; an unknown one falls back
    to the generic prefix. The fallback exists so a new setting needs no
    edit here, and the map exists so the known ones do not read as if the
    fallback were the rule.
    """
    return ENV_NAMES.get(name, ENV_VAR + "_" + name.upper().replace("-", "_"))


def local_file(project_root: Path, name: str) -> Path:
    """The local, ignored setting file of one workspace."""
    return Path(project_root) / ".pair" / name


def project_file(project_root: Path, name: str) -> Path:
    """The committed policy file of the project.

    The name is `.pair-<name>`, the trunk's name (`.pair-trunk`): the
    local file lives INSIDE the ignored state dir, and the committed one
    sits beside it with the same prefix, so the pair reads as one thing —
    `.pair/trunk` and `.pair-trunk`, `.pair/merge-strategy` and
    `.pair-merge-strategy`.
    """
    return Path(project_root) / (".pair-" + name)


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


def resolve(
    project_root: Path | str | None,
    name: str,
    default: str = "",
) -> str:
    """The project's value for the setting `name`, or `default`.

    `project_root` is the project directory (not the `.pair` state dir): the
    settings live beside the code, the state dir holds the runtime. `None`
    means the current directory.

    The order — environment, local file, committed file, default — is the
    trunk's order (`trunk.resolve_trunk`), and the reasons are the same. The
    first hit wins; a later source never overrules an earlier one.
    """
    root = Path(project_root) if project_root is not None else Path.cwd()
    env = os.environ.get(env_var(name), "").strip()
    if env:
        return env
    for path in (local_file(root, name), project_file(root, name)):
        value = _first_line(path)
        if value:
            return value
    return default


def resolve_list(
    project_root: Path | str | None,
    name: str,
    default: list[str],
    separator: str = ",",
) -> list[str]:
    """The project's value for the setting `name`, as a list of names.

    The same order as `resolve`, but the value is a list: the first line of
    the file, split on the separator, with the blanks dropped.

    A list is a setting, not a sentence: "the caches we share" is a fact about
    paths, and a path is not guessed from prose. The separator is a comma, and
    the names are stripped, so `.venv, node_modules` and `.venv,node_modules`
    are the same list.
    """
    raw = resolve(project_root, name)
    if not raw:
        return list(default)
    items = [part.strip() for part in raw.split(separator)]
    names = [item for item in items if item]
    return names or list(default)
