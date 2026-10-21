"""Diagnostics tests (`bin/pairdoctor`).

Checked for what the parsing fix was about: the unittest summary lives in
stderr, and reading stdout alone gave an empty tail forever.

The function loads via AST without executing the file: at top level
pairdoctor immediately runs checks (print + subprocess), and exec of the
whole file hangs running test_protocol inside the import.
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_fn(name):
    import json as _json
    import sys as _sys
    tree = ast.parse((ROOT / "bin" / "pairdoctor").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            mod = ast.Module(body=[node], type_ignores=[])
            ns: dict = {"sys": _sys, "json": _json, "Path": Path, "ROOT": ROOT}
            exec(compile(mod, "pairdoctor_fn", "exec"), ns)
            return ns[name]
    raise AssertionError(name + " not found in bin/pairdoctor")


unittest_tail = _load_fn("unittest_tail")
pilot_lines = _load_fn("pilot_lines")
freshness_lines = _load_fn("freshness_lines")


class TestUnittestTail(unittest.TestCase):
    def test_stderr_carries_the_count(self) -> None:
        """unittest writes Ran/OK to stderr: the tail lives in the merged output."""
        self.assertEqual(["Ran 3 tests in 0.011s", "OK"],
                         unittest_tail("", "Ran 3 tests in 0.011s\nOK\n"))

    def test_stdout_alone_is_not_enough(self) -> None:
        """Reading stdout alone — empty tail: the card’s original defect."""
        self.assertEqual([], unittest_tail("", ""))

    def test_failure_tail_is_found(self) -> None:
        self.assertIn("FAILED (failures=1)",
                      unittest_tail("", "Ran 3 tests in 0.011s\nFAILED (failures=1)\n"))


class TestDeliverySection(unittest.TestCase):
    def test_section_present(self) -> None:
        text = (ROOT / "bin" / "pairdoctor").read_text(encoding="utf-8")
        self.assertIn("[5] delivery", text)
        self.assertIn("composer_line", text)
        self.assertIn("is_composer_empty", text)

    def test_sections_renumbered(self) -> None:
        text = (ROOT / "bin" / "pairdoctor").read_text(encoding="utf-8")
        self.assertIn("[7] git", text)


class TestPilotSection(unittest.TestCase):
    def test_section_present(self) -> None:
        text = (ROOT / "bin" / "pairdoctor").read_text(encoding="utf-8")
        self.assertIn("[8] pilot", text)
        self.assertIn("pilot_lines", text)

    def test_dead_pilot_warns(self) -> None:
        """No pid file, no matching process: the section must warn (round 92 C5)."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            lines = pilot_lines([Path(tmp) / ".pair"])
            self.assertTrue(lines)
            self.assertTrue(all(level == "warn" for level, _ in lines))
            self.assertIn("supervisor dead", lines[0][1])

    def test_live_pid_file_reports_ok(self) -> None:
        """Own pid alive: ok. Uses our own pid — no process to spawn."""
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sd = Path(tmp) / ".pair"
            sd.mkdir()
            (sd / "pilot.pid").write_text(str(os.getpid()), encoding="utf-8")
            lines = pilot_lines([sd])
            self.assertEqual([level for level, _ in lines], ["ok"])

    def test_foreign_pilot_does_not_count(self) -> None:
        """A live pairpilot for ANOTHER state dir must not read as ours.
        Measured: nyx-system pid 2293899 alive while umputum pilot dead.
        The pgrep output carries a foreign --state: the match must be on
        OUR dir string, so this stays warn. Would go ok if the code ever
        matched on the bare 'pairpilot' name."""
        import subprocess
        import tempfile
        real_run = subprocess.run

        def fake_run(cmd, **kwargs):
            class Out:
                stdout = "2293899 python3 /home/alexios/work/nyx-system/bin/pairpilot --state /home/alexios/work/nyx-system/.pair\n"
            return Out()

        subprocess.run = fake_run
        try:
            with tempfile.TemporaryDirectory() as tmp:
                sd = Path(tmp) / ".pair"
                sd.mkdir()
                lines = pilot_lines([sd])
        finally:
            subprocess.run = real_run
        self.assertEqual([level for level, _ in lines], ["warn"])

class TestRound86Fixes(unittest.TestCase):
    def test_server_key_from_profile(self) -> None:
        text = (ROOT / "bin" / "pairdoctor").read_text(encoding="utf-8")
        self.assertIn('spec.get("server_key", "pair")', text)

    def test_none_is_warn_not_bad(self) -> None:
        text = (ROOT / "bin" / "pairdoctor").read_text(encoding="utf-8")
        self.assertIn("composer busy (not visible) — delivery deferred", text)
        # no bare bad() next to None
        idx = text.find("if _line is None:")
        block = text[idx:idx + 300]
        self.assertNotIn("bad(", block)

    def test_targets_deduped(self) -> None:
        text = (ROOT / "bin" / "pairdoctor").read_text(encoding="utf-8")
        self.assertIn("if _proj not in targets:", text)

    def test_freshness_section_present(self) -> None:
        text = (ROOT / "bin" / "pairdoctor").read_text(encoding="utf-8")
        self.assertIn("[6] freshness", text)
        self.assertIn("list_newer_than_start", text)

    def test_freshness_compares_against_servers(self) -> None:
        """The section must count from the server start, not the doctor’s.

        Measured 2026-10-13: list_stale_files() without process_start_ts
        compares against /proc/self — the doctor just launched, the list is
        always empty, the section green on a stale server."""
        text = (ROOT / "bin" / "pairdoctor").read_text(encoding="utf-8")
        self.assertIn("process_start_ts=", text)
        # no bare list_newer_than_start() without an argument next to the call
        import re
        bare = [m for m in re.finditer(r"list_newer_than_start\(\)", text)]
        self.assertEqual([], bare, "a bare call counts the doctor start, not the servers")

    def test_freshness_section_calls_freshness_lines(self) -> None:
        """Round 87: the [6] section routes through freshness_lines(targets).

        Structural (AST), not a substring: walks the module body outside any
        def and requires a Call to freshness_lines. Goes red if the section
        is rewired to a hardcoded ok or a bare list_stale_files().
        """
        tree = ast.parse((ROOT / "bin" / "pairdoctor").read_text(encoding="utf-8"))
        calls = []

        class Visitor(ast.NodeVisitor):
            def visit_FunctionDef(self, node):
                pass  # the def itself does not count; only its callers do

            def visit_Call(self, node):
                func = node.func
                if isinstance(func, ast.Name) and func.id == "freshness_lines":
                    calls.append(node)
                self.generic_visit(node)

        Visitor().visit(tree)
        self.assertEqual(1, len(calls), "section [6] must call freshness_lines once")
        self.assertTrue(any(isinstance(a, ast.Name) and a.id == "targets"
                            for a in calls[0].args),
                        "the call must pass the live targets, not a fixture")

    def test_freshness_section_fires_through_its_own_call(self) -> None:
        """Round 87: the section is tested through its own call, not the library.

        A sandbox state.json carries the live pid; a probe .py newer than the
        process start must surface as warn. Goes red if the section is rewired
        back to a bare list_stale_files() (doctor-fresh by construction).
        """
        import json
        import os
        import sys
        import tempfile
        sys.path.insert(0, str(ROOT / "mcp" / "pair_mailbox"))
        import server_freshness as fresh
        with tempfile.TemporaryDirectory() as tmp:
            code = Path(tmp) / "code"
            code.mkdir()
            probe = code / "probe.py"
            probe.write_text("x = 1\n", encoding="utf-8")
            now = probe.stat().st_mtime
            os.utime(probe, (now + 30, now + 30))
            st = {"roles": {"srv": {"pid": os.getpid()}}}
            box = Path(tmp) / ".pair"
            box.mkdir()
            (box / "state.json").write_text(json.dumps(st), encoding="utf-8")
            level, msg = freshness_lines([box], code_dir=code)[0]
            self.assertEqual("warn", level)
            self.assertIn("probe.py", msg)
            self.assertNotEqual([], fresh.list_stale_files(
                code_dir=code, process_start_ts=now - 60))


if __name__ == "__main__":
    unittest.main(verbosity=2)
