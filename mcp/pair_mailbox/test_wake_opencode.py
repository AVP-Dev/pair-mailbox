"""Fail-closed tests for exact OpenCode session wake routing."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import wake_opencode
from pair_test_base import FakeOpencode


class WakeExactSession(unittest.TestCase):
    def setUp(self) -> None:
        self.service = mock.patch("wake_opencode.service_endpoint",
                                  return_value=("http://service", "pw"))
        self.service.start()
        self.addCleanup(self.service.stop)

    def test_missing_session_id_refuses_without_listing_or_prompting(self) -> None:
        with mock.patch("wake_opencode.subprocess.run") as run:
            result = wake_opencode.wake("/project", "pointer", None)
        self.assertFalse(result["woken"])
        self.assertIn("no recorded", result["why"])
        run.assert_not_called()

    def test_unavailable_session_refuses_without_candidate_fallback(self) -> None:
        with mock.patch("wake_opencode._get", return_value={}), \
                mock.patch("wake_opencode.subprocess.run") as run:
            result = wake_opencode.wake("/project", "pointer", "ses_missing")
        self.assertFalse(result["woken"])
        self.assertIn("missing or stale", result["why"])
        run.assert_not_called()

    def test_two_same_project_sessions_still_target_only_recorded_id(self) -> None:
        session_id = "ses_recipient"
        session = {"data": {"id": session_id,
                            "location": {"directory": "/project"}}}
        accepted = '{"data":{"type":"user","payload":{"text":"hello"}}}'
        with mock.patch("wake_opencode._get", return_value=session) as get, \
                mock.patch("wake_opencode.subprocess.run") as run:
            run.return_value.stdout = accepted
            result = wake_opencode.wake("/project", "pointer", session_id)
        self.assertTrue(result["woken"])
        self.assertEqual(
            "http://service/api/session/ses_recipient",
            get.call_args.args[0],
            "the adapter verifies the exact session, not the session list",
        )
        posted = " ".join(str(part) for part in run.call_args.args)
        self.assertIn("/api/session/ses_recipient/prompt", posted)
        self.assertNotIn('/api/session"', posted)

    def test_foreign_project_session_refuses(self) -> None:
        session = {"data": {"id": "ses_other",
                            "location": {"directory": "/other"}}}
        with mock.patch("wake_opencode._get", return_value=session), \
                mock.patch("wake_opencode.subprocess.run") as run:
            result = wake_opencode.wake("/project", "pointer", "ses_other")
        self.assertFalse(result["woken"])
        self.assertIn("another project", result["why"])
        run.assert_not_called()

    def test_encoded_session_id_is_used_for_both_requests(self) -> None:
        session_id = "ses/a"
        session = {"data": {"id": session_id,
                            "location": {"directory": "/project"}}}
        accepted = '{"data":{"type":"user"}}'
        with mock.patch("wake_opencode._get", return_value=session) as get, \
                mock.patch("wake_opencode.subprocess.run") as run:
            run.return_value.stdout = accepted
            result = wake_opencode.wake("/project", "pointer", session_id)
        self.assertTrue(result["woken"])
        self.assertIn("ses%2Fa", get.call_args.args[0])
        self.assertIn("ses%2Fa", " ".join(str(part) for part in run.call_args.args))


class WakeTheOwningServer(unittest.TestCase):
    """The wake against the server that actually holds the pair's connection.

    Round 141: the shared background service is not that server. The pair runs
    OpenCode standalone, so the endpoint is found in the process tree and the
    session comes from the server's own active session -- the session the
    agent that has never called a tool is in.
    """

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

    def test_the_endpoint_is_the_server_that_holds_the_pair(self) -> None:
        fake = self.start_fake(active={"id": "ses_active",
                                       "location": {"directory": str(self.project)}})
        endpoint = wake_opencode.service_endpoint(fake.child_pid)
        url, password = endpoint
        self.assertEqual(fake.url(), url)
        self.assertEqual(fake.password, password)

    def test_the_wake_steers_the_active_session_of_that_server(self) -> None:
        fake = self.start_fake(active={"id": "ses_active",
                                       "location": {"directory": str(self.project)}})
        result = wake_opencode.wake(self.project, "the pointer", None,
                                    role_pid=fake.child_pid)
        self.assertTrue(result["woken"], result["why"])
        posted = fake.requests()
        self.assertEqual(1, len(posted))
        self.assertEqual("/api/session/ses_active/prompt", posted[0]["path"])
        self.assertIn("the pointer", posted[0]["body"])

    def test_the_wake_refuses_an_active_session_of_another_project(self) -> None:
        fake = self.start_fake(active={"id": "ses_foreign",
                                       "location": {"directory": str(self.other)}})
        result = wake_opencode.wake(self.project, "the pointer", None,
                                    role_pid=fake.child_pid)
        self.assertFalse(result["woken"])
        self.assertIn("another project", result["why"])
        self.assertEqual([], fake.requests(), "nothing is typed into a foreign session")

    def test_the_wake_falls_back_to_the_recorded_id(self) -> None:
        fake = self.start_fake(
            active=None,
            sessions={"ses_recorded": {"id": "ses_recorded",
                                       "location": {"directory": str(self.project)}}})
        result = wake_opencode.wake(self.project, "the pointer", "ses_recorded",
                                    role_pid=fake.child_pid)
        self.assertTrue(result["woken"], result["why"])
        posted = fake.requests()
        self.assertEqual("/api/session/ses_recorded/prompt", posted[0]["path"])

    def test_the_wake_without_a_session_refuses(self) -> None:
        fake = self.start_fake(active=None)
        result = wake_opencode.wake(self.project, "the pointer", None,
                                    role_pid=fake.child_pid)
        self.assertFalse(result["woken"])
        self.assertIn("no recorded", result["why"])
        self.assertEqual([], fake.requests())


if __name__ == "__main__":
    unittest.main()
