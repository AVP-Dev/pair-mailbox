"""Dashboard CLI tests (`bin/pairboard`).

What the CLI exists for checks, not what it does inside:

- **never finds state where none stands, saying so by code, not a blank page.**
  A silent blank page is the worst answer to "show me the pair".
- **never touches state.** An observer changing what it watches reads worse than
  none; the check compares `.pair/` content before and after.
- **resolves `--state` from the project root.** From the current dir the path
  drifts, and a human launching from a subdir takes "pair not found".
- **`--watch` survives a broken pass.** One fallen pass must never kill watching:
  the disk file stays the prior one, and the page says so.

Real launches run as a subprocess, because `signal` and `sys.exit` never check by
calling `main()` in the same process: the SIGINT handler never fires on itself,
and the check would pass stopping nothing.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PAIRBOARD = REPO_ROOT / "bin" / "pairboard"
SNAPSHOT_RE = ('<script id="snapshot" type="application/json">', "</script>")


class CliTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state = Path(self._tmp.name) / ".pair"
        self.state.mkdir(parents=True)
        (self.state / "state.json").write_text(json.dumps({
            "task": "dashboard", "mode": "fix", "autonomy": "auto", "writer": "agy",
            "phase": "implement", "round": 16, "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
            "roles": {"agy": {"pid": 1, "pane": "", "last_seen": "2026-01-01T00:00:00+00:00"}},
            "tasks": [{"id": 1, "text": "task", "status": "open", "round": 16}],
        }, ensure_ascii=False), encoding="utf-8")
        self.out = Path(self._tmp.name) / "board.html"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_cli(self, *args: str, expect: int | None = 0) -> subprocess.CompletedProcess:
        proc = subprocess.run([sys.executable, str(PAIRBOARD), *args],
                              cwd=str(REPO_ROOT), capture_output=True, text=True,
                              timeout=60)
        if expect is not None:
            self.assertEqual(proc.returncode, expect,
                             f"code {proc.returncode}\nstdout: {proc.stdout}\nstderr: {proc.stderr}")
        return proc

    def snapshot_of(self, page: str) -> dict:
        start = page.index(SNAPSHOT_RE[0]) + len(SNAPSHOT_RE[0])
        return json.loads(page[start:page.index(SNAPSHOT_RE[1], start)])


class BuildOnceTest(CliTestCase):
    def test_default_out_is_the_repo_root(self):
        """The default showcase stands at the repo root, never deep in .pair.

        The board-to-root-and-live card item 1: the showcase forgets because it
        lies deep, opening by hand. A root default is a one-liner, and this test
        holds it.
        """
        import importlib.machinery
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "pairboard_mod", str(PAIRBOARD),
            loader=importlib.machinery.SourceFileLoader(
                "pairboard_mod", str(PAIRBOARD)))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(module.DEFAULT_OUT, "board.html")
        self.assertFalse(module.DEFAULT_OUT.startswith(".pair/"))

    def test_writes_page_and_prints_openable_url(self):
        proc = self.run_cli("--state", str(self.state), "--out", str(self.out))
        self.assertTrue(self.out.exists())
        self.assertIn(f"file://{self.out}", proc.stdout)
        self.assertEqual(self.snapshot_of(self.out.read_text(encoding="utf-8"))
                         ["round"]["number"], 16)

    def test_one_shot_says_it_is_not_live(self):
        # The human must know the picture never refreshes alone: or he will watch a
        # frozen tape taking the pair for silent.
        proc = self.run_cli("--state", str(self.state), "--out", str(self.out))
        self.assertIn("--watch", proc.stdout)
        # Only `<head>` checks. The client mentions `<meta http-equiv="refresh">` in its
        # comment and selector, so a whole-file search would find the explanation, not a
        # working auto-refresh.
        head = self.out.read_text(encoding="utf-8").split("</head>")[0]
        self.assertIsNone(re.search(r'<meta\s+http-equiv="refresh"', head),
                          "a one-shot run must never enable auto-refresh")

    def test_relative_out_is_taken_from_project_root(self):
        # Launching from a foreign dir must never scatter the file over the machine.
        proc = subprocess.run([sys.executable, str(PAIRBOARD),
                               "--state", str(self.state), "--out", "board-test.html"],
                              cwd=self._tmp.name, capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        written = REPO_ROOT / "board-test.html"
        try:
            self.assertTrue(written.exists())
            self.assertIn(str(written), proc.stdout)
        finally:
            written.unlink(missing_ok=True)

    def test_missing_state_is_refused_with_a_reason(self):
        empty = Path(self._tmp.name) / "empty"
        empty.mkdir()
        proc = self.run_cli("--state", str(empty), "--out", str(self.out), expect=2)
        self.assertIn("state.json", proc.stderr)
        self.assertFalse(self.out.exists(), "a page with no state is a lie")

    def test_state_is_never_modified(self):
        def contents() -> dict[str, bytes]:
            # Files only: `Store` creates service dirs (mailbox, notes, patches, obligations),
            # and reading a dir in bytes falls with IsADirectoryError — checking the wrong thing.
            # `.lock` is the protocol own service file: `Store` creates it on access, the server
            return {path.name: path.read_bytes() for path in self.state.iterdir()
                    if path.is_file()}

        before = contents()
        self.run_cli("--state", str(self.state), "--out", str(self.out))
        after = contents()
        # does exactly the same. Excluded for cause, never "just in case": the "nothing
        # writes" demand covers pair state, while `.lock` is no state but the access lock to
        # it. All the rest must match byte for byte.
        # it. All the rest must match byte for byte.
        # must match byte for byte.
        lock = {".lock"}
        self.assertEqual({k: v for k, v in after.items() if k not in lock},
                         {k: v for k, v in before.items() if k not in lock},
                         "the dashboard changed the state it watches")
        self.assertEqual(set(after) - set(before), lock,
                         "the dashboard created something in .pair/ besides the service .lock")

    def test_print_json_writes_no_html(self):
        proc = self.run_cli("--state", str(self.state), "--print-json")
        data = json.loads(proc.stdout)
        self.assertEqual(data["round"]["writer"], "agy")
        self.assertFalse(self.out.exists())

    def test_limit_bounds_the_feed(self):
        transcript = self.state / "transcript.jsonl"
        with transcript.open("w", encoding="utf-8") as handle:
            for index in range(30):
                handle.write(json.dumps({"kind": "send", "role": "agy", "to": "a",
                                         "subject": f"letter {index}"}) + "\n")
        proc = self.run_cli("--state", str(self.state), "--print-json", "--limit", "5")
        data = json.loads(proc.stdout)
        self.assertEqual(len(data["events"]), 5)
        self.assertEqual(data["counts"]["events_total"], 30)

    def test_stale_after_reaches_the_snapshot(self):
        proc = self.run_cli("--state", str(self.state), "--print-json",
                            "--stale-after", "42")
        self.assertEqual(json.loads(proc.stdout)["stale_after"], 42)


class WatchTest(CliTestCase):
    """`--watch` checks by subprocess, on facts only.

    A signal catches only in the already-launched interpreter main thread. Sending
    SIGINT while Python still loads kills the process with code -2, the handler
    never standing in time: a race of the TEST against launch, not program
    behaviour. So the first file always awaits before stopping — it appears
    exactly past the handler install.
    """

    def start_watch(self, seconds: str = "1") -> subprocess.Popen:
        proc = subprocess.Popen(
            [sys.executable, str(PAIRBOARD), "--state", str(self.state),
             "--out", str(self.out), "--watch", seconds],
            cwd=str(REPO_ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self._stop, proc)
        return proc

    def wait_for_page(self, timeout: float = 40) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.out.exists():
                page = self.out.read_text(encoding="utf-8")
                if page.strip():
                    return page
            time.sleep(0.2)
        self.fail("--watch never created the file in time")

    def test_watch_embeds_refresh_and_keeps_rewriting(self):
        self.start_watch()
        first = self.wait_for_page()
        head = first.split("</head>")[0]
        self.assertIn('<meta http-equiv="refresh" content="1">', head)
        self.assertTrue(self.snapshot_of(first)["watch"])

        # An event in the journal must reach the page with no relaunch: that is the `--watch`
        # meaning.
        self.state.joinpath("transcript.jsonl").write_text(
            json.dumps({"kind": "send", "role": "agy", "to": "a",
                        "subject": "appeared on air"}) + "\n", encoding="utf-8")
        deadline = time.time() + 45
        seen = False
        while time.time() < deadline and not seen:
            seen = "appeared on air" in self.out.read_text(encoding="utf-8")
            if not seen:
                time.sleep(0.3)
        self.assertTrue(seen, "--watch never picked a new event")

    def _stop(self, proc: subprocess.Popen) -> None:
        """Awaits readiness, stops, checks the return code.

        The code matters: `run_watch` catches SIGINT on purpose, leaving no temp
        file and never returning 130. Or the gate would read a stop as a fall.
        """
        if proc.poll() is None and not self.out.exists():
            self.wait_for_page(timeout=40)
        if proc.poll() is None:
            proc.send_signal(2)  # SIGINT
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
                self.fail("--watch never stopped on SIGINT")
        self.assertEqual(proc.returncode, 0,
                         f"stop code {proc.returncode}; "
                         f"a temp file stayed: {list(self.out.parent.glob('*.tmp'))}")

    def test_interrupted_watch_leaves_no_temporary_file(self):
        proc = self.start_watch("3600")
        self.wait_for_page()
        self._stop(proc)
        self.assertTrue(self.out.exists())
        self.assertEqual(list(self.out.parent.glob("*.tmp")), [])

    def test_watch_survives_a_failing_pass(self):
        # While the agent lives, state drops mid-work: watching must never fall because a
        # prior whole page stayed on disk.
        proc = self.start_watch()
        self.wait_for_page()
        self.state.joinpath("state.json").unlink()
        self.state.mkdir(exist_ok=True)
        time.sleep(4)
        self.assertIsNone(proc.poll(), "--watch died past a failed pass")
        head = self.out.read_text(encoding="utf-8").split("</head>")[0]
        self.assertIn('<meta http-equiv="refresh"', head)


class UsageTest(unittest.TestCase):
    def test_help_works(self):
        proc = subprocess.run([sys.executable, str(PAIRBOARD), "--help"],
                              cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("--watch", proc.stdout)
        self.assertIn("--print-json", proc.stdout)

    def test_script_is_executable_and_has_a_shebang(self):
        # bin/pairwatch is the model: the size gate counts the file a source exactly because
        # it owns an executable bit and a shebang.
        self.assertTrue(os.access(PAIRBOARD, os.X_OK), "bin/pairboard is not executable")
        self.assertTrue(PAIRBOARD.read_text(encoding="utf-8").startswith("#!"))


if __name__ == "__main__":
    unittest.main(verbosity=2)