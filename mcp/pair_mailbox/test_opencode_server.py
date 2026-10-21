"""The opencode server that holds the pair's connection is found, not guessed.

Round 141: the wake adapter used to ask the CLI for the SHARED background
service, which is not the server a standalone pair talks to, and the session id
it steered by was one the client had sent in a tool call's metadata -- which
arrives only after the agent has called something. The agent that most needs
waking is the one that has never called anything.

These tests stand a real opencode server process up (a fake one, in
`pair_test_base.py`) and check that the discovery finds it, that it refuses a
pid whose start time does not match, and that the session it binds is the
server's own active session and only when that session is ours.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import opencode_server
from pair_test_base import FakeOpencode

SERVER = Path(__file__).resolve().parent / "server.py"


def session(session_id: str, directory: Path) -> dict:
    return {"id": session_id, "location": {"directory": str(directory)}}


class OwningServer(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.other = self.root / "other-project"
        self.other.mkdir()

    def start_fake(self, active: dict | None = None,
                   sessions: dict | None = None) -> FakeOpencode:
        fake = FakeOpencode(self.root, active=active, sessions=sessions)
        fake.start()
        self.addCleanup(fake.stop)
        return fake

    def test_the_owning_server_is_found_in_the_process_tree(self) -> None:
        fake = self.start_fake(active=session("ses_active", self.project))
        found = opencode_server.find_opencode_server(fake.child_pid)
        self.assertIsNotNone(found, "the server that spawned the pair is in the tree")
        self.assertEqual(fake.password, found["password"])
        self.assertEqual([fake.url()], found["urls"])

    def test_the_shared_service_is_not_mistaken_for_the_owning_server(self) -> None:
        fake = self.start_fake(active=session("ses_active", self.project))
        # The pair's own process is not below an opencode server: nothing is
        # found, and the caller falls back to the shared service.
        self.assertIsNone(opencode_server.find_opencode_server(os.getpid()))

    def test_a_recycled_pid_finds_nothing(self) -> None:
        fake = self.start_fake(active=session("ses_active", self.project))
        found = opencode_server.find_opencode_server(fake.child_pid, pid_start=1)
        self.assertIsNone(found, "a pid without its start time proves nothing")

    def test_bind_session_names_the_active_session_of_this_project(self) -> None:
        fake = self.start_fake(active=session("ses_active", self.project))
        self.assertEqual("ses_active",
                         opencode_server.bind_session(self.project, fake.child_pid))

    def test_bind_session_refuses_a_session_of_another_project(self) -> None:
        fake = self.start_fake(active=session("ses_foreign", self.other))
        self.assertEqual("",
                         opencode_server.bind_session(self.project, fake.child_pid))

    def test_bind_session_is_empty_without_a_server(self) -> None:
        self.assertEqual("", opencode_server.bind_session(self.project, os.getpid()))

    def test_the_active_session_is_the_servers_own_answer(self) -> None:
        fake = self.start_fake(active=session("ses_active", self.project))
        found = opencode_server.find_opencode_server(fake.child_pid)
        answered = opencode_server.active_session(found["urls"][0], found["password"])
        self.assertEqual("ses_active", answered["id"])

    def test_the_active_session_is_not_answered_without_the_password(self) -> None:
        fake = self.start_fake(active=session("ses_active", self.project))
        found = opencode_server.find_opencode_server(fake.child_pid)
        self.assertIsNone(opencode_server.active_session(found["urls"][0], "wrong"))


class ThePairServerBindsTheSession(unittest.TestCase):
    """The pair's own server binds the session id at MCP initialize.

    The opencode client sends no metadata at initialize (measured: its
    initialize request carries no `_meta` at all), so the id the wake needs can
    only come from the server that owns the connection. Here the REAL pair
    server runs as a child of the fake opencode server, which is the process
    tree the production case has.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.project = self.root / "project"
        self.state = self.project / ".pair"
        self.state.mkdir(parents=True)
        self.other = self.root / "other-project"
        self.other.mkdir()

    def start_pair(self, active: dict | None) -> FakeOpencode:
        fake = FakeOpencode(
            self.root, active=active,
            mcp_argv=[sys.executable, str(SERVER), "--role", "opencode",
                      "--state", str(self.state)])
        fake.start()
        self.addCleanup(fake.stop)
        return fake

    def state_roles(self) -> dict:
        return json.loads((self.state / "state.json").read_text(encoding="utf-8"))["roles"]

    def test_the_role_entry_carries_the_session_id_after_initialize(self) -> None:
        fake = self.start_pair({"id": "ses_active",
                                "location": {"directory": str(self.project)}})
        roles = self.state_roles()
        self.assertEqual("ses_active", roles["opencode"]["opencode_session_id"])
        reply = json.loads(fake.reply_file.read_text(encoding="utf-8"))
        self.assertEqual("pair-mailbox", reply["result"]["serverInfo"]["name"])

    def test_no_session_of_another_project_is_bound(self) -> None:
        self.start_pair({"id": "ses_foreign",
                         "location": {"directory": str(self.other)}})
        self.assertNotIn("opencode_session_id", self.state_roles()["opencode"])


if __name__ == "__main__":
    unittest.main()
