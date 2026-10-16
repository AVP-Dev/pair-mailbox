from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import read_wait

from pair_test_base import PairTestCase  # noqa: E402
from server import PairSession  # noqa: E402


class ReadWaitBudget(unittest.TestCase):
    def test_omp_profile_allows_full_protocol_wait(self) -> None:
        timeout = read_wait.client_timeout_ms("omp")
        self.assertEqual(65_000, timeout)
        self.assertEqual((60.0, False), read_wait.effective_read_wait(60, timeout))

    def test_short_explicit_override_is_reported_as_capped(self) -> None:
        with mock.patch.dict("os.environ", {"PAIR_CLIENT_TIMEOUT": "20000"}):
            timeout = read_wait.client_timeout_ms("omp")
        self.assertEqual(20_000, timeout)
        self.assertEqual((15.0, True), read_wait.effective_read_wait(45, timeout))

    def test_unconfigured_role_uses_conservative_default(self) -> None:
        self.assertEqual(20_000, read_wait.client_timeout_ms("custom-role"))

    def test_missing_or_broken_profile_uses_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cli-profiles.json"
            self.assertEqual(20_000, read_wait.client_timeout_ms("omp", profiles_path=path))
            path.write_text("{broken", encoding="utf-8")
            self.assertEqual(20_000, read_wait.client_timeout_ms("omp", profiles_path=path))

    def test_structured_timeout_uses_execution_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cli-profiles.json"
            path.write_text(json.dumps({"test": {"mcp": {"entry": {
                "timeout": {"startup": 10_000, "execution": 70_000},
            }}}}), encoding="utf-8")
            self.assertEqual(70_000, read_wait.client_timeout_ms("test", profiles_path=path))


class ReadWaitSession(PairTestCase):
    def test_pair_read_uses_its_role_profile_timeout(self) -> None:
        session = PairSession("omp", self.store)
        received = []
        def budget(wait, timeout_ms):
            received.append(timeout_ms)
            return 0.0, False
        with mock.patch.dict("os.environ", {}, clear=True), \
                mock.patch("server.effective_read_wait", side_effect=budget), \
                mock.patch.object(session, "_drain", return_value=([], None)):
            answer = session.tool_pair_read(wait_seconds=45)
        self.assertEqual([65_000], received)
        self.assertNotIn("wait_capped_by_client_timeout", answer)


if __name__ == "__main__":
    unittest.main()
