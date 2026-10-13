#!/usr/bin/env python3
"""Tests for the human-only read commands: `pairctl all/why`, project overview.

Match `pairctl_read.py`. The `bin/pairchat` check rides here too, because it
prints the overview from the same module: checking them apart would raise the
same fixture twice for the same code.

Run:  python3 mcp/pair_mailbox/test_pairctl_read.py
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pair_test_base import PAIRCHAT, PairTestCase  # noqa: E402
from pairctl_read import (  # noqa: E402
    discover_projects,
    project_row,
    render_table,
    silence_seconds,
)
from server import Store, main  # noqa: E402

class TestMultiProject(PairTestCase):
    """See all projects at once, not switching windows.

    The `pairchat --once` regression rides here too: the flag never existed,
    yet I kept advising to type it. The test runs the script as a subprocess
    and demands it exit on its own — otherwise documentation lies again.
    """

    def _project(self, name: str, *, phase: str = "scoping", writer=None, events: int = 0):
        root = Path(self._tmp.name) / name / ".pair"
        store = Store(root)
        store.mutate(lambda state: state.update({"phase": phase, "writer": writer,
                                                 "task": f"task {name}"}))
        for _ in range(events):
            store.append_transcript("claim", writer or "opencode", writer=writer or "opencode")
        return root

    def setUp(self) -> None:
        super().setUp()
        # discover_projects never loses the project you stand in by design — and we stand
        # in the repo that has .pair. Without chdir every test would also see umputum.
        self._cwd = os.getcwd()
        os.chdir(self._tmp.name)

    def tearDown(self) -> None:
        os.chdir(self._cwd)
        os.environ.pop("PAIR_SCAN", None)
        super().tearDown()

    def test_discovers_only_directories_that_actually_have_state(self) -> None:
        good = self._project("good", events=1)
        # a dir with no state.json is not a project and must stay out of the table
        (Path(self._tmp.name) / "empty" / ".pair").mkdir(parents=True)
        os.environ["PAIR_SCAN"] = str(Path(self._tmp.name) / "*" / ".pair")
        try:
            found = discover_projects()
        finally:
            del os.environ["PAIR_SCAN"]
        self.assertIn(good.resolve(), found)
        self.assertNotIn((Path(self._tmp.name) / "empty" / ".pair").resolve(), found)

    def test_pair_scan_replaces_the_default_globs(self) -> None:
        inside = self._project("inside", events=1)
        os.environ["PAIR_SCAN"] = str(Path(self._tmp.name) / "inside" / ".pair")
        try:
            found = discover_projects()
        finally:
            del os.environ["PAIR_SCAN"]
        self.assertEqual(found, [inside.resolve()])

    def test_accepts_a_project_directory_not_only_the_pair_directory(self) -> None:
        self._project("proj", events=1)
        found = discover_projects([str(Path(self._tmp.name) / "proj")])
        self.assertEqual(found, [(Path(self._tmp.name) / "proj" / ".pair").resolve()])

    def test_same_project_given_twice_appears_once(self) -> None:
        root = self._project("twice", events=1)
        found = discover_projects([str(root), str(root), str(root)])
        self.assertEqual(found, [root.resolve()])

    def test_cwd_project_survives_an_empty_pair_scan(self) -> None:
        # wherever the human stands, its own project must stay in view
        root = self._project("here", events=1)
        cwd = os.getcwd()
        os.chdir(root.parent)
        os.environ["PAIR_SCAN"] = str(Path(self._tmp.name) / "nothing-matches")
        try:
            found = discover_projects()
        finally:
            del os.environ["PAIR_SCAN"]
            os.chdir(cwd)
        self.assertIn(root.resolve(), found)

    def test_silence_is_none_when_nothing_was_ever_recorded(self) -> None:
        self.assertIsNone(silence_seconds(self._project("quiet")))

    def test_silence_counts_from_the_last_event(self) -> None:
        root = self._project("noisy", events=3)
        age = silence_seconds(root)
        self.assertIsNotNone(age)
        self.assertGreaterEqual(age, 0.0)
        self.assertLess(age, 60.0)

    def test_silence_ignores_state_writes_that_changed_nothing(self) -> None:
        # writing state.json with zero events: the pair never moved, so silence must
        # not read as fresh activity
        root = self._project("idle", events=1)
        Store(root).mutate(lambda state: state.update({"task": "touching state"}))
        self.assertLess(silence_seconds(root), 60.0)

    def test_a_finished_project_is_never_reported_as_stuck(self) -> None:
        root = self._project("finished", phase="done", writer="opencode", events=1)
        row = project_row(root, stuck_min=0)
        self.assertTrue(row["closed"])
        self.assertFalse(row["stuck"], "a done project is not stalled, however long it has been quiet")

    def test_an_unfinished_project_that_went_quiet_is_stuck(self) -> None:
        root = self._project("stalled", phase="verify", writer="opencode", events=1)
        row = project_row(root, stuck_min=0)
        self.assertFalse(row["closed"])
        self.assertTrue(row["stuck"])

    def test_table_columns_line_up_for_long_project_names(self) -> None:
        self._project("a-very-long-project-name-here", events=1)
        self._project("tiny", events=1)
        os.environ["PAIR_SCAN"] = str(Path(self._tmp.name) / "*" / ".pair")
        try:
            rows = [project_row(root) for root in discover_projects()]
        finally:
            del os.environ["PAIR_SCAN"]
        lines = render_table(rows).splitlines()
        self.assertGreaterEqual(len(lines), 4)

        def columns(line: str, count: int = 5) -> list:
            # the last column may hold spaces, so compare only the first five starts:
            # those must align across rows
            return [match.start() for match in re.finditer(r"\S+", line)][:count]

        reference = columns(lines[0])
        self.assertEqual(len(reference), 5)
        for line in lines[2:]:
            if line.strip():
                self.assertEqual(columns(line), reference,
                                 "columns drifted:\n" + "\n".join(lines))

    def test_ctl_all_gives_one_row_per_project(self) -> None:
        self._project("alpha", events=1)
        self._project("beta", phase="done", writer="agy", events=1)
        os.environ["PAIR_SCAN"] = str(Path(self._tmp.name) / "*" / ".pair")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["all"])
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertIn("alpha", text)
        self.assertIn("beta", text)
        # exactly our two projects, nothing extra from the default globs
        table = text.split("\n\n")[0].splitlines()
        names = [line.split()[0] for line in table[2:] if line.strip()]
        self.assertEqual(sorted(names), ["alpha", "beta"])

    def test_ctl_all_json_is_machine_readable(self) -> None:
        self._project("gamma", events=1)
        os.environ["PAIR_SCAN"] = str(Path(self._tmp.name) / "*" / ".pair")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            main(["all", "--json"])
        rows = json.loads(out.getvalue())
        self.assertEqual([row["project"] for row in rows], ["gamma"])
        for row in rows:
            for key in ("project", "phase", "writer", "silence", "stuck", "closed", "path"):
                self.assertIn(key, row)

    def test_ctl_all_signals_stall_with_a_distinct_exit_code(self) -> None:
        # 3 — so the script can tell "all alive" from "someone stalled"
        self._project("dead", phase="verify", writer="opencode", events=1)
        os.environ["PAIR_SCAN"] = str(Path(self._tmp.name) / "*" / ".pair")
        try:
            code = main(["all", "--stuck-min", "0"])
        finally:
            del os.environ["PAIR_SCAN"]
        self.assertEqual(code, 3)

    def test_ctl_all_says_where_to_look_when_there_is_nothing(self) -> None:
        os.environ["PAIR_SCAN"] = str(Path(self._tmp.name) / "nothing-matches")
        cwd = os.getcwd()
        os.chdir(self._tmp.name)
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = main(["all"])
        finally:
            del os.environ["PAIR_SCAN"]
            os.chdir(cwd)
        self.assertEqual(code, 1)
        self.assertIn("PAIR_SCAN", out.getvalue())

    def test_pairchat_once_exits_on_its_own(self) -> None:
        # exactly what I got wrong: no flag existed, the script hung till Ctrl-C
        self.store.mutate(lambda state: state.update({"phase": "done", "writer": "writer-agent"}))
        self.store.append_transcript("claim", "writer-agent", writer="writer-agent")
        (self.root / "notes").mkdir(parents=True, exist_ok=True)
        (self.root / "notes" / "plan.md").write_text("two-line plan\n", encoding="utf-8")
        env = dict(os.environ, PAIR_STATE=str(self.root))
        try:
            done = subprocess.run([sys.executable, str(PAIRCHAT), "--once"],
                                  capture_output=True, text=True, timeout=30, env=env)
        except subprocess.TimeoutExpired:
            self.fail("--once never exited on its own: the script hangs as before the flag")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("WRITER: writer-agent", done.stdout)
        self.assertIn("two-line plan", done.stdout)

    def test_naming_a_project_replaces_the_built_in_search(self) -> None:
        # otherwise "look at ~/work/blog" would show five glob projects plus blog
        named = self._project("named", events=1)
        self.assertEqual(discover_projects([str(named)]), [named.resolve()])

    def test_pairchat_all_prints_a_header_and_labels_every_project(self) -> None:
        for name in ("proj-one", "proj-two"):
            self._project(name, events=1)
        done = subprocess.run(
            [sys.executable, str(PAIRCHAT), "--all", "--once"],
            capture_output=True, text=True, timeout=30,
            env=dict(os.environ, PAIR_SCAN=str(Path(self._tmp.name) / "*" / ".pair")),
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        plain = re.sub(r"\x1b\[[0-9;]*m", "", done.stdout)
        self.assertIn("proj-one", plain)
        self.assertIn("proj-two", plain)
        self.assertIn("phase", plain.splitlines()[0])
        for line in plain.splitlines():
            if "CLAIM" in line or "ADVANCE" in line:
                head = line.split("CLAIM")[0].split("ADVANCE")[0].strip()
                self.assertRegex(head, r"^(proj-one|proj-two)\s+\[\d\d:\d\d:\d\d\]$",
                                 "no project label on the block: " + line)

    def test_table_words_follow_the_canon(self) -> None:
        """R3 seq 1167: the table vocabulary is pinned, not advisory.

        pairchat and pairctl_read print two tables from one module; a
        near-miss spelling (not_started, Stuck) would pass every other
        test while the human reads two dictionaries.
        """
        rows = [{"project": "p", "phase": "implement", "mode": "fix",
                 "writer": "agy", "peers": ["opencode"], "silence_seconds": None,
                 "silence": "", "stuck": False, "reason": "", "task": "t"},
                {"project": "q", "phase": "implement", "mode": "fix",
                 "writer": "agy", "peers": [], "silence_seconds": 3600,
                 "silence": "60 min", "stuck": True, "reason": "", "task": "t"}]
        text = render_table(rows)
        for word in ("project", "phase", "mode", "writer", "critic",
                     "silence", "why", "task", "not started", "STUCK"):
            self.assertIn(word, text, f"canon word missing: {word}")

    def test_pairchat_shows_only_the_project_you_are_in(self) -> None:
        # The user asked exactly this: in the project folder — that project’s
        # dialogue, no strangers. Mixed output harms here.
        mine = self._project("mine", events=1)
        self._project("theirs", events=1)
        os.environ["PAIR_SCAN"] = str(Path(self._tmp.name) / "*" / ".pair")
        cwd = os.getcwd()
        os.chdir(mine.parent)
        # The subprocess inherits the environment: another PAIR_STATE from the shell (and
        # the pair always has one) overrides cwd in pick_roots, and the test sees the
        # live project instead of the temp one. Scrubbed — the cwd search is checked.
        env = dict(os.environ)
        env.pop("PAIR_STATE", None)
        try:
            done = subprocess.run([sys.executable, str(PAIRCHAT), "--once"],
                                  capture_output=True, text=True, timeout=30, env=env)
        finally:
            del os.environ["PAIR_SCAN"]
            os.chdir(cwd)
        self.assertEqual(done.returncode, 0, done.stderr)
        plain = re.sub(r"\x1b\[[0-9;]*m", "", done.stdout)
        self.assertIn("mine", plain)
        self.assertNotIn("theirs", plain, "stranger project shown without --all")

    def test_pairchat_outside_a_project_points_back_at_the_project(self) -> None:
        self._project("somewhere-else", events=1)
        cwd = os.getcwd()
        os.chdir(self._tmp.name)  # no .pair here
        env = dict(os.environ)
        env.pop("PAIR_STATE", None)
        try:
            done = subprocess.run([sys.executable, str(PAIRCHAT), "--once"],
                                  capture_output=True, text=True, timeout=30, env=env)
        finally:
            os.chdir(cwd)
        self.assertEqual(done.returncode, 1)
        text = done.stdout + done.stderr
        self.assertIn("--all", text, "never told what to do to see it all")
        self.assertIn("cd", text, "never told to enter the project folder")

    def test_agent_notes_are_shown_by_default_and_work_notes_are_not(self) -> None:
        # Agent notes (plan, finding...) are work, they show.
        # My work notes about pair-mailbox itself — not, they run to hundreds of code lines.
        # backlog and finding-<ID> are prescribed by §9 and the skills: their absence
        # from the output is the same defect fixed in pairchat (round-33 verdict).
        for kind in ("plan", "finding", "finding-12", "backlog", "verdict",
                     "z-scratch", "x-wrap"):
            path = self.root / "notes" / f"{kind}.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"note content {kind}\n", encoding="utf-8")
        self.store.mutate(lambda state: state.update({"phase": "plan"}))
        self.store.append_transcript("note", "writer-agent", note_kind="plan", chars=10)
        env = dict(os.environ, PAIR_STATE=str(self.root))
        cwd = os.getcwd()
        os.chdir(self.root.parent)
        try:
            done = subprocess.run([sys.executable, str(PAIRCHAT), "--once"],
                                  capture_output=True, text=True, timeout=30, env=env)
            loud = subprocess.run([sys.executable, str(PAIRCHAT), "--all-notes", "--once"],
                                  capture_output=True, text=True, timeout=30, env=env)
        finally:
            os.chdir(cwd)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("note content plan", done.stdout)
        self.assertIn("note content verdict", done.stdout)
        self.assertIn("note content backlog", done.stdout)
        self.assertIn("note content finding-12", done.stdout)
        self.assertNotIn("z-scratch", done.stdout, "work note leaked into the output")
        self.assertNotIn("x-wrap", done.stdout, "work note leaked into the output")
        self.assertIn("note content z-scratch", loud.stdout,
                      "--all-notes must also show work notes")

    def test_long_lines_are_wrapped_to_a_readable_width(self) -> None:
        # Agents write 300+ char paragraphs; without wrapping the terminal tears them
        # mid-word.
        self.store.mutate(lambda state: state.update({"phase": "plan"}))
        (self.root / "mailbox" / "peer").mkdir(parents=True, exist_ok=True)
        payload = " ".join(["word%02d" % i for i in range(60)])
        (self.root / "mailbox" / "peer" / "000001-t.json").write_text(
            json.dumps({"seq": 1, "ts": "2026-10-02T20:00:00+00:00",
                        "from": "peer", "to": "writer-agent", "subject": "long subject " + payload,
                        "text": payload, "label": "Chat from peer: "}),
            encoding="utf-8")
        env = dict(os.environ, PAIR_STATE=str(self.root))
        done = subprocess.run([sys.executable, str(PAIRCHAT), "--once"],
                              capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual(done.returncode, 0, done.stderr)
        plain = re.sub(r"\x1b\[[0-9;]*m", "", done.stdout)
        for line in plain.splitlines():
            self.assertLessEqual(len(line), 105,
                                 "line never wrapped: " + repr(line[:120]))

    def test_refuses_when_there_is_no_state(self) -> None:
        empty = Path(self._tmp.name) / "no-project"
        empty.mkdir()
        done = subprocess.run(
            [sys.executable, str(PAIRCHAT), "--once"],
            capture_output=True, text=True, timeout=30,
            env=dict(os.environ, PAIR_STATE=str(empty / ".pair")),
        )
        self.assertEqual(done.returncode, 1)
        self.assertIn("not in a project folder", done.stdout + done.stderr)

    def test_state_resolution_prefers_flag_over_env_over_cwd(self) -> None:
        """Three components resolved the path three ways — a second state in one
        project (state-dir-not-pinned). Priority: --state, then PAIR_STATE."""
        from pairctl_read import extract_state
        explicit = str(self.root)
        self.assertEqual(extract_state(["--state", explicit, "status"])[0], Path(explicit))
        env = dict(os.environ, PAIR_STATE=explicit)
        old = os.environ.get("PAIR_STATE")
        os.environ["PAIR_STATE"] = explicit
        try:
            self.assertEqual(extract_state(["status"])[0], Path(explicit))
        finally:
            if old is None:
                os.environ.pop("PAIR_STATE", None)
            else:
                os.environ["PAIR_STATE"] = old
        self.assertNotEqual(explicit, str(Path(".pair").resolve()))

    def test_nested_cwd_resolves_to_project_root_state(self) -> None:
        """Launching from a subdir creates no second .pair: walk-up to
        .pair/state.json (state-dir-not-pinned, OBJECT-2)."""
        from pairctl_read import extract_state, walk_up_to_state
        proj = Path(self._tmp.name) / "proj"
        (proj / ".pair").mkdir(parents=True)
        (proj / ".pair" / "state.json").write_text("{}")
        deep = proj / "a" / "b"
        deep.mkdir(parents=True)
        old_env = os.environ.pop("PAIR_STATE", None)
        cwd = os.getcwd()
        os.chdir(deep)
        try:
            self.assertEqual(walk_up_to_state(), (proj / ".pair").resolve())
            self.assertEqual(extract_state(["status"])[0], (proj / ".pair").resolve())
        finally:
            os.chdir(cwd)
            if old_env is not None:
                os.environ["PAIR_STATE"] = old_env

    def test_extract_pane_prefers_flag_over_env(self) -> None:
        """The explicit --pane/PAIR_PANE channel extracts from args and beats env."""
        from pairctl_read import extract_pane
        self.assertEqual(extract_pane(["--pane", "%42", "status"]), ("%42", ["status"]))
        self.assertEqual(extract_pane(["--pane=%42", "status"]), ("%42", ["status"]))
        self.assertEqual(extract_pane(["status"]), ("", ["status"]))
        old = os.environ.get("PAIR_PANE")
        os.environ["PAIR_PANE"] = "%99"
        try:
            self.assertEqual(extract_pane(["status"]), ("%99", ["status"]))
            self.assertEqual(extract_pane(["--pane", "%42", "status"]), ("%42", ["status"]))
            self.assertEqual(extract_pane(["--pane=%42", "status"]), ("%42", ["status"]))
        finally:
            if old is None:
                os.environ.pop("PAIR_PANE", None)
            else:
                os.environ["PAIR_PANE"] = old


if __name__ == "__main__":
    unittest.main(verbosity=2)
