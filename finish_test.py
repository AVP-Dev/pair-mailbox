"""The maintenance entry point must use enrolled launchers and stop on failure."""
from __future__ import annotations

import importlib.util
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

LOADER = SourceFileLoader("finish_entry", str(Path(__file__).with_name("finish.sh")))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
finish = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(finish)


class FinishCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.calls = []
        self.addCleanup(patch.stopall)
        patch.object(finish, "ROOT", self.root).start()
        patch.object(finish, "run", side_effect=self.fake_run).start()

    def fake_run(self, argv, **kwargs):
        self.calls.append(([str(arg) for arg in argv], kwargs))
        return SimpleNamespace(returncode=0)

    def test_pair_uses_single_enrolled_launcher_with_task_and_autonomy(self):
        self.assertEqual(finish.main(["--task", "literal $() task", "--autonomy", "semi"]), 0)
        argv, kwargs = self.calls[-1]
        self.assertEqual(argv[:3], [str(self.root / "bin/pairup"), "--state", str(self.root / ".pair")])
        self.assertEqual(argv[-1], "autonomy: semi\nliteral $() task")
        self.assertEqual(kwargs["cwd"], str(self.root))
        self.assertFalse(any("pairpilot" in arg for call, _ in self.calls for arg in call))

    def test_duo_uses_enrolled_launcher(self):
        self.assertEqual(finish.main(["--duo", "--cli", "codex", "--task", "check"]), 0)
        self.assertEqual(self.calls[-1][0], [str(self.root / "bin/pair-duo"), "--state",
                                          str(self.root / ".pair"), "codex", "check"])

    def test_verify_failure_stops_before_install(self):
        with patch.object(finish, "run", return_value=SimpleNamespace(returncode=2)) as run:
            self.assertEqual(finish.main([]), 2)
            self.assertEqual(run.call_count, 1)

    def test_no_pair_does_not_launch_agents(self):
        self.assertEqual(finish.main(["--no-pair"]), 0)
        self.assertEqual(len(self.calls), 2)

    def test_no_pair_still_applies_explicit_autonomy(self):
        self.assertEqual(finish.main(["--no-pair", "--autonomy", "semi"]), 0)
        self.assertEqual(self.calls[-1][0][-2:], ["autonomy", "semi"])
        self.assertEqual(len(self.calls), 3)


if __name__ == "__main__":
    unittest.main()
