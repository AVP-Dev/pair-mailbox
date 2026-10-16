"""Client-aware timeout budget for the bounded mailbox read."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Mapping

MAX_READ_WAIT_SECONDS = 60
DEFAULT_READ_WAIT_SECONDS = 0.0
READ_WAIT_MARGIN_SECONDS = 5.0
DEFAULT_CLIENT_TIMEOUT_MS = 20_000
PROFILE_PATH = Path(__file__).resolve().parents[2] / "cli-profiles.json"


def client_timeout_ms(
    role: str,
    *,
    profiles_path: Path = PROFILE_PATH,
    environ: Mapping[str, str] | None = None,
) -> int:
    """Resolve the actual MCP client timeout, preferring an explicit override.

    The server is one process per role, so the checked-in client profile is the
    right source when the launcher does not provide ``PAIR_CLIENT_TIMEOUT``.
    Missing or damaged profile data falls back conservatively to OpenCode's
    documented 20s default; it never prevents the mailbox from being read.
    """
    env = os.environ if environ is None else environ
    override = env.get("PAIR_CLIENT_TIMEOUT")
    if override is not None:
        value = int(override)
        return value if value > 0 else DEFAULT_CLIENT_TIMEOUT_MS

    try:
        profiles = json.loads(profiles_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return DEFAULT_CLIENT_TIMEOUT_MS
    timeout = (((profiles.get(role) or {}).get("mcp") or {}).get("entry") or {}).get("timeout")
    if isinstance(timeout, dict):
        timeout = timeout.get("execution")
    try:
        value = int(timeout)
    except (TypeError, ValueError):
        return DEFAULT_CLIENT_TIMEOUT_MS
    return value if value > 0 else DEFAULT_CLIENT_TIMEOUT_MS


def effective_read_wait(wait: float, timeout_ms: int) -> tuple[float, bool]:
    """Return a wait that stays inside both the protocol and caller's timeout."""
    capped = min(float(wait), float(MAX_READ_WAIT_SECONDS))
    client_budget = max(0.0, (int(timeout_ms) / 1000.0) - READ_WAIT_MARGIN_SECONDS)
    effective = min(capped, client_budget)
    return effective, effective < capped
