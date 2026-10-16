"""Fail-closed tests for exact OpenCode session wake routing."""
from __future__ import annotations

import unittest
from unittest import mock

import wake_opencode


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


if __name__ == "__main__":
    unittest.main()
