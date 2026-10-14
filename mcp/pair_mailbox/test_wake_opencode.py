"""wake_opencode: контракт fail-closed и форма запроса. Живые API-доказательства — в finding-ноте."""
from __future__ import annotations

import json
import unittest
from unittest import mock

import wake_opencode


class WakeFailClosed(unittest.TestCase):
    def test_no_service_means_no_wake(self) -> None:
        with mock.patch("wake_opencode.service_endpoint", return_value=("", "")):
            self.assertEqual(wake_opencode.wake("/x", "указатель")["woken"], False)

    def test_no_fresh_sessions_is_a_refuse(self) -> None:
        with mock.patch("wake_opencode.service_endpoint", return_value=("http://x", "pw")), \
                mock.patch("wake_opencode.candidate_sessions", return_value=[]):
            result = wake_opencode.wake("/x", "указатель")
            self.assertFalse(result["woken"])
            self.assertIn("no fresh", result["why"])

    def test_ambiguous_candidates_are_a_refuse(self) -> None:
        two = ["ses_a", "ses_b"]
        with mock.patch("wake_opencode.service_endpoint", return_value=("http://x", "pw")), \
                mock.patch("wake_opencode.candidate_sessions", return_value=two):
            result = wake_opencode.wake("/x", "указатель")
            self.assertFalse(result["woken"])
            self.assertIn("ambiguous", result["why"])

    def test_single_candidate_is_steered(self) -> None:
        ok = '{"data":{"type":"user","payload":{"text":"привет"}}}'
        with mock.patch("wake_opencode.service_endpoint", return_value=("http://x", "pw")), \
                mock.patch("wake_opencode.candidate_sessions", return_value=["ses_one"]), \
                mock.patch("subprocess.run") as run:
            run.return_value.stdout = ok
            result = wake_opencode.wake("/home/proj", "[pair] новое сообщение")
            self.assertTrue(result["woken"])
            posted = run.call_args[0][0]
            self.assertTrue(any("/api/session/ses_one/prompt" in str(part) for part in posted))

    def test_project_root_filters_other_sessions(self) -> None:
        sessions = {"data": [
            {"id": " чужой".strip(), "location": {"directory": "/другой"},
             "time": {"idle": __import__("time").time() * 1000}},
            {"id": "наш", "location": {"directory": "/home/proj"},
             "time": {"idle": __import__("time").time() * 1000}},
        ]}
        with mock.patch("wake_opencode.service_endpoint", return_value=("http://x", "pw")), \
                mock.patch("wake_opencode._get", return_value=sessions):
            got = wake_opencode.candidate_sessions("/home/proj", "http://x", ("opencode", "pw"))
            self.assertEqual(got, ["наш"])


if __name__ == "__main__":
    unittest.main()
