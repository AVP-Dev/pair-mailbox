"""Role workspace persistence and project-root resolution for MCP launches."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from resume_prompt import initial_prompt
from store_core import Store, _git_common_dir, _git_toplevel, utcnow


def project_root_for(store: Store, role: str) -> Path:
    """Prefer the role's verified checkout over a long-lived daemon cwd."""
    base = (store.root.parent if store.root.name == ".pair" else store.root).resolve()
    common = _git_common_dir(base)
    try:
        roles = store.read().get("roles", {})
        entry = roles.get(role, {}) if isinstance(roles, dict) else {}
        value = entry.get("workspace") if isinstance(entry, dict) else None
        candidate = Path(value).resolve() if isinstance(value, str) and value else None
    except (OSError, RuntimeError, TypeError):
        candidate = None
    if candidate is not None and candidate.is_dir():
        if common is None and candidate == base:
            return base
        top = _git_toplevel(candidate)
        if top is not None and common is not None and _git_common_dir(top) == common:
            return top
    return store.project_root


def record_role_workspace(store: Store, role: str, workspace: Path) -> Path | None:
    """Persist a launch directory only when it is the same repository as this room."""
    base = (store.root.parent if store.root.name == ".pair" else store.root).resolve()
    candidate = _git_toplevel(workspace)
    common = _git_common_dir(base)
    if candidate is None and common is None:
        candidate = workspace.resolve()
        if candidate != base:
            return None
    if candidate is None or common is None or _git_common_dir(candidate) != common:
        if not (common is None and candidate == base):
            return None
    resolved = candidate.resolve()

    def save(state: dict[str, Any]) -> None:
        roles = state.setdefault("roles", {})
        entry = roles.setdefault(role, {"first_seen": utcnow(), "reads": 0, "sends": 0})
        entry["workspace"] = str(resolved)

    store.mutate(save)
    return resolved


def launch_state(state_path: Path, role: str, workspace: Path) -> dict[str, Any]:
    """Record the verified launch checkout before reading role launch settings."""
    store = Store(state_path)
    record_role_workspace(store, role, workspace)
    return store.read()
