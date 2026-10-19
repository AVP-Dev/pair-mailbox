#!/usr/bin/env python3
"""One waiting rule on every shipped surface.

The measured failure: the shipped templates said "call again" and "waiting is
work", and a critic read its mailbox in a loop for a whole night — 29k polls,
each one a full context, for nothing. A surface that teaches the loop teaches
it to every session that reads it, so the wording is guarded here: the old rule
must not come back, and every surface that states the rule must state the SAME
one.

The surfaces are the files an agent is actually given. The scan is wider: any
tracked text in the repo that tells an agent to loop again is a defect, wherever
it hides.
"""
from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# The surfaces that STATE the waiting contract. Each one must say the same rule.
CONTRACT_SURFACES = (
    "skills/pair-work/SKILL.md",
    "skills/pair-work/SKILL.ru.md",
    "skills/pair-chat/SKILL.md",
    ".agents/agents/pair-writer/agent.md",
    ".agents/agents/pair-critic/agent.md",
    ".opencode/agents/writer.md",
    "docs/PROTOCOL.md",
    "docs/PROTOCOL.ru.md",
    "README.md",
    "README.ru.md",
)

# The old rule, in the words it was written in. It must not come back.
FORBIDDEN = (
    "call again",
    "waiting is work",
    "marks it read",
    "зови снова",
    "ожидание — это работа",
    "ожидание - это работа",
)

# The one rule, in the two languages the repo ships in.
ONE_READ_MARK = ("one read per turn", "одно чтение за ход")


def tracked_text_files() -> list[Path]:
    """Every tracked text file, minus the state dir, the vendored trees and THIS file.

    This file is excluded on purpose: it NAMES the forbidden wording (that is what
    it hunts for), so a scan that included it would find the phrases in the
    hunter's own list. The guard covers the shipped surfaces, not the guard.
    """
    out = subprocess.run(
        ["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True, check=True)
    this_file = Path(__file__).resolve()
    files = []
    for name in out.stdout.splitlines():
        if not name:
            continue
        if name.startswith((".pair/", "node_modules/")):
            continue
        path = (REPO_ROOT / name).resolve()
        if path == this_file:
            continue
        if not name.endswith((".md", ".py", ".ts", ".json", ".jsonc", ".sh", ".ru.md")):
            continue
        files.append(path)
    return files


class TestWaitingContract(unittest.TestCase):
    def test_no_shipped_text_teaches_the_read_loop(self) -> None:
        for path in tracked_text_files():
            try:
                text = path.read_text(encoding="utf-8").lower()
            except (OSError, UnicodeDecodeError):
                continue
            for phrase in FORBIDDEN:
                self.assertNotIn(phrase, text,
                                 f"{path.relative_to(REPO_ROOT)} still teaches the "
                                 f"read loop: {phrase!r}")

    def test_every_surface_states_the_same_rule(self) -> None:
        for name in CONTRACT_SURFACES:
            path = REPO_ROOT / name
            self.assertTrue(path.exists(), f"{name} is a shipped surface")
            text = path.read_text(encoding="utf-8").lower()
            self.assertTrue(
                any(mark in text for mark in ONE_READ_MARK),
                f"{name} does not state the one-read rule")

    def test_every_surface_names_the_token_that_ends_the_batch(self) -> None:
        """Reading without the token leaves the mail unacknowledged forever."""
        for name in CONTRACT_SURFACES:
            path = REPO_ROOT / name
            text = path.read_text(encoding="utf-8")
            self.assertIn("ack_token", text,
                          f"{name} talks about reading without naming the token")


if __name__ == "__main__":
    unittest.main()
