from __future__ import annotations

import unittest

from bin_test_coverage import audit, has_test_sibling


class BinTestCoverageTests(unittest.TestCase):
    def test_missing_sibling_fails_with_named_file(self) -> None:
        errors = audit({"bin/new-tool"}, {})
        self.assertEqual(1, len(errors))
        self.assertIn("bin/new-tool", errors[0])

    def test_test_sibling_satisfies_executable(self) -> None:
        paths = {"bin/new-tool", "bin/new-tool_test.py"}
        self.assertTrue(has_test_sibling("bin/new-tool", paths))
        self.assertEqual([], audit(paths, {}))

    def test_reasoned_exemption_satisfies_executable(self) -> None:
        paths = {"bin/new-tool"}
        self.assertEqual([], audit(paths, {"bin/new-tool": "legacy wrapper is covered elsewhere"}))

    def test_blank_and_stale_exemptions_fail(self) -> None:
        errors = audit({"bin/new-tool"}, {"bin/new-tool": " ", "bin/gone": "obsolete"})
        self.assertTrue(any("non-empty reason" in error for error in errors))
        self.assertTrue(any("stale exemption" in error for error in errors))

    def test_python_executable_accepts_test_prefix_convention(self) -> None:
        self.assertTrue(has_test_sibling("bin/tool.py", {"bin/test_tool.py"}))


if __name__ == "__main__":
    unittest.main()
