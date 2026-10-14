#!/usr/bin/env python3
"""Installer tests.

They check not "the file exists" but that install NEVER BREAKS foreign files: the
only place where a bug costs the user data. Each test runs the real install.sh
in a separate HOME looking at the result.

Run:  python3 install_test.py
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
INSTALL = ROOT / "install.sh"

sys.path.insert(0, str(ROOT))
import importlib.util as _ilu


def _install_mod():
    from importlib.machinery import SourceFileLoader
    loader = SourceFileLoader("install_sh", str(INSTALL))
    spec = _ilu.spec_from_loader("install_sh", loader)
    mod = _ilu.module_from_spec(spec)
    mod.__dict__["__file__"] = str(INSTALL)
    loader.exec_module(mod)
    return mod


_INSTALL = _install_mod()

def run_install(home: Path, *args: str) -> subprocess.CompletedProcess:
    # The nested test_protocol.py run (~3 min) stays out: the verify layer runs the
    # suite separately, while every install would carry it anew (698s per file).
    # One test below checks the skip works, and one that the suite truly launches
    # with no skip.
    env = dict(os.environ, HOME=str(home), PAIR_INSTALL_SKIP_SUITE="1")
    return subprocess.run([sys.executable, str(INSTALL), *args], capture_output=True,
                          text=True, env=env, cwd=str(ROOT), timeout=300)


def _roles_claiming(raw: str | None, pane: str) -> set[str]:
    """Roles recording this pane.

    Exactly the pane owner must check, not the mere fact of presence: the launching
    suite process pane lawfully stands in the roster when the suite runs from the
    pair own pane, and "stands in the roster" versus "belongs to this role" read
    different claims. A broken json gives an empty set: the check must never fall
    over reading wrong, not over anything the installer did.
    """
    if not raw:
        return set()
    try:
        roles = json.loads(raw).get("roles") or {}
    except (ValueError, AttributeError):
        return set()
    return {name for name, info in roles.items()
            if isinstance(info, dict) and info.get("pane") == pane}


def _dead_roles(raw: str | None) -> set[str]:
    """Roles with no live process: such never appear or vanish alone.

    The pair writes into live state right during install — its last_seen, its
    registrations — so "a role appeared" alone proves nothing: it could be the
    suite own agent. The reverse proves: a role with a dead pid could appear or
    vanish by no hand but the installer. Exactly this regression (verify writing
    into the real state.json, found live in round 31) the dead-role comparing
    catches, while live self-registration never drops it.
    """
    if not raw:
        return set()
    try:
        roles = json.loads(raw).get("roles") or {}
    except (ValueError, AttributeError):
        return set()
    out = set()
    for name, info in roles.items():
        pid = (info or {}).get("pid") if isinstance(info, dict) else None
        alive = False
        if isinstance(pid, int) and pid > 0:
            try:
                os.kill(pid, 0)
                alive = True
            except ProcessLookupError:
                alive = False
            except PermissionError:
                alive = True  # a foreign process: nothing to check with, counts as living
            except OSError:
                alive = False
        if not alive:
            out.add(name)
    return out


class InstallCase(unittest.TestCase):
    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp(prefix="pair-install-"))
        self.addCleanup(shutil.rmtree, self.home, True)
        cfg = self.home / ".config" / "opencode"
        cfg.mkdir(parents=True)
        self.jsonc = cfg / "opencode.jsonc"

    def write_jsonc(self, text: str) -> None:
        self.jsonc.write_text(text, encoding="utf-8")

    def read_jsonc(self) -> dict:
        return json.loads(self.jsonc.read_text(encoding="utf-8"))

    def agents(self) -> Path:
        return self.home / ".config" / "opencode" / "AGENTS.md"


class TestForeignFilesSurvive(InstallCase):
    """The installer promises in its header: "foreign config keys stay untouched".

    The promise checks here, not by reading code."""

    def test_unrelated_keys_in_jsonc_survive(self) -> None:
        self.write_jsonc(json.dumps({
            "$schema": "https://opencode.ai/config.json",
            "my_plugin": {"setting": "value"},
            "permission": {"bash": {"sudo *": "deny"}},
        }, indent=2))
        run_install(self.home, "opencode")
        cfg = self.read_jsonc()
        self.assertEqual(cfg.get("my_plugin"), {"setting": "value"})
        self.assertEqual(cfg["permission"]["bash"]["sudo *"], "deny",
                         "human deny rules must survive install")

    def test_jsonc_comments_do_not_break_the_install(self) -> None:
        """jsonc means JSON with comments. `json.loads` fell on such a file, and install
        broke off leaving half the work."""
        self.write_jsonc(
            '{\n  "$schema": "https://opencode.ai/config.json",\n'
            '  // my comment\n  "model": "test-model",\n}\n')
        result = run_install(self.home, "opencode")
        self.assertNotIn("unreadable", result.stdout,
                         "a comment in jsonc must never drop install")
        cfg = _INSTALL.parse_jsonc(self.jsonc.read_text(encoding="utf-8"))
        self.assertEqual(cfg.get("model"), "test-model")

    def test_urls_inside_strings_are_not_mangled(self) -> None:
        """Comments strip by a state machine, never a regex: or "https://..." inside a
        string splits in half."""
        self.write_jsonc(json.dumps({"server": "https://example.com/a//b"}, indent=2))
        run_install(self.home, "opencode")
        self.assertEqual(self.read_jsonc()["server"], "https://example.com/a//b")

    def test_jsonc_comments_survive_the_install(self) -> None:
        """json.dumps rewrote the file whole wiping human comments — against the "foreign
        keys untouched" promise. Only the pair block patches."""
        self.write_jsonc(
            '{\n  // my comment\n  "model": "test-model",\n'
            '  /* block */\n  "mcp": {\n    // inside mcp\n    "context7": {"url": "https://x/a//b"},\n  },\n}\n')
        run_install(self.home, "opencode")
        raw = self.jsonc.read_text(encoding="utf-8")
        self.assertIn("// my comment", raw)
        self.assertIn("/* block */", raw)
        self.assertIn("// inside mcp", raw)
        self.assertIn("https://x/a//b", raw)
        cfg = _INSTALL.parse_jsonc(self.jsonc.read_text(encoding="utf-8"))
        self.assertEqual(cfg["model"], "test-model")
        self.assertIn("pair", cfg["mcp"])
        self.assertIn("context7", cfg["mcp"])

    def test_second_install_keeps_comments_and_replaces_pair(self) -> None:
        """A repeat never duplicates the block nor eats comments: replace, not insert."""
        self.write_jsonc('{\n  // keep me\n  "model": "m",\n}\n')
        run_install(self.home, "opencode")
        first = self.jsonc.read_text(encoding="utf-8")
        run_install(self.home, "opencode")
        second = self.jsonc.read_text(encoding="utf-8")
        self.assertIn("// keep me", second)
        self.assertEqual(second.count('"pair"'), 1)
        self.assertEqual(first, second)


    def test_instructions_tail_after_the_marker_is_kept(self) -> None:
        """The old code ran split(MARK)[0] deleting all past the marker — silent, every
        launch. Deleting foreign-file content needs asking per §12."""
        self.agents().write_text(
            "# My rules\n\nleave alone.\n\n<!-- pair-mailbox -->\n"
            "## Pair Work\n\nold block\n\n"
            "## My section past the marker\n\nthis matters and must never drop.\n",
            encoding="utf-8")
        run_install(self.home, "opencode")
        text = self.agents().read_text(encoding="utf-8")
        self.assertIn("My section past the marker", text,
                      "the tail past our block belongs to the human")
        self.assertIn("must never drop", text)

    def test_a_backup_is_written_before_overwriting(self) -> None:
        self.agents().write_text("# was\n\n<!-- pair-mailbox -->\nold\n", encoding="utf-8")
        run_install(self.home, "opencode")
        backup = self.agents().with_suffix(".md.bak")
        self.assertTrue(backup.exists(), "overwriting a human file needs a copy")
        self.assertIn("was", backup.read_text(encoding="utf-8"))

    def test_reinstall_is_idempotent(self) -> None:
        """Idempotence stands declared in the installer header."""
        self.agents().write_text("# my rules\n", encoding="utf-8")
        run_install(self.home, "opencode")
        first = self.agents().read_text(encoding="utf-8")
        run_install(self.home, "opencode")
        self.assertEqual(first, self.agents().read_text(encoding="utf-8"),
                         "a second install must never change the file")

    def test_unrelated_mcp_servers_survive(self) -> None:
        self.write_jsonc(json.dumps({
            "mcp": {"context7": {"type": "http", "url": "https://example.com"}},
        }, indent=2))
        run_install(self.home, "opencode")
        self.assertIn("context7", self.read_jsonc()["mcp"],
                      "a foreign MCP server must never vanish")

    def test_broken_config_is_left_alone_and_says_so(self) -> None:
        """An unreadable config must stay untouched and named, never silently overwritten
        with half a setup."""
        original = '{ this is not json at all\n'
        self.write_jsonc(original)
        result = run_install(self.home, "opencode")
        self.assertIn("unreadable", result.stdout)
        self.assertIn("left untouched", result.stdout)
        self.assertEqual(self.jsonc.read_text(encoding="utf-8"), original,
                         "an unreadable config never rewrites")


class TestNativePermissions(InstallCase):
    """The installer writes a native permission layer: an allow-list instead of clicks.

    The round-54 pain: 5 of 6 profiles rose with a bare launch, deny wrote for
    opencode only. The check runs on files in an isolated HOME and on the project
    .claude/settings.json: the layer stands, deny holds the §12 categories.
    """

    def test_agy_settings_carry_deny(self) -> None:
        result = run_install(self.home, "agy")
        self.assertIn("permissions:", result.stdout)
        path = self.home / ".gemini" / "antigravity-cli" / "settings.json"
        self.assertTrue(path.exists(), "Antigravity settings never written")
        perms = json.loads(path.read_text(encoding="utf-8"))["permissions"]
        self.assertIn("command(sudo)", perms["deny"])
        self.assertIn("command(git status)", perms["allow"])

    def test_codex_is_left_to_the_pilot(self) -> None:
        """codex owns no native allow/deny — the installer never touches it.

        The profile stays on the pilot (§13b): a round-54 decision, not an
        undone bit, and silently writing an approval-"never" into config.toml
        stays banned.
        """
        result = run_install(self.home, "codex")
        self.assertNotIn("permissions:", result.stdout)

    def test_claude_project_settings_keep_deny(self) -> None:
        run_install(self.home, "claude")
        path = ROOT / ".claude" / "settings.json"
        self.assertTrue(path.exists(), "the project .claude/settings.json is lost")
        perms = json.loads(path.read_text(encoding="utf-8"))["permission"]
        self.assertIn("Bash(sudo:*)", perms["deny"])
        self.assertIn("Bash(git status:*)", perms["allow"])


class TestMcpRegistration(InstallCase):
    def test_pair_server_is_registered_and_verified(self) -> None:
        result = run_install(self.home, "opencode")
        self.assertIn("pair-mailbox", result.stdout)
        argv = self.read_jsonc()["mcp"]["pair"]["command"]
        self.assertEqual(argv[0], "sh")
        self.assertTrue(argv[1].endswith("serve.sh"))
        self.assertEqual(argv[2], "opencode", "a role sews into config, not process memory")

    def test_the_suite_runs_as_part_of_the_install(self) -> None:
        if os.environ.get("PAIR_INSTALL_FAST"):
            self.skipTest("fast mode: the integration layer runs the live suite")
        env = dict(os.environ, HOME=str(self.home))
        env.pop("PAIR_INSTALL_SKIP_SUITE", None)
        result = subprocess.run([sys.executable, str(INSTALL), "opencode"],
                                capture_output=True, text=True, env=env,
                                cwd=str(ROOT), timeout=600)
        self.assertNotIn("TESTS FAILED", result.stdout)

    def test_test_installs_skip_the_nested_suite(self) -> None:
        """run_install sets PAIR_INSTALL_SKIP_SUITE: the nested ~3 min per test install
        never repeat (it was 698s per file)."""
        result = run_install(self.home, "opencode")
        self.assertNotIn("server tests", result.stdout)

    def test_skills_installation_includes_pair_chat(self) -> None:
        """Invariant 5: install.sh lays every public skill into skills_dir."""
        run_install(self.home, "opencode")
        skills_dir = self.home / ".config" / "opencode" / "skills"
        # The full source set, not two names: the next added skill must not
        # vanish silently the way pair-chat did (round 92 C2).
        want = {p.name for p in (ROOT / "skills").iterdir() if p.is_dir()}
        have = {p.name for p in skills_dir.iterdir() if p.is_dir()}
        self.assertEqual(want - have, set(),
                         "skills missing from the target skills_dir: " + str(want - have))
        self.assertTrue((skills_dir / "pair-chat" / "SKILL.md").exists(),
                        "the pair-chat skill must stand in the target skills_dir")


class TestVerifyDoesNotTouchTheRoster(InstallCase):
    """A transport check holds no right to change live-pair state.

    Found live, not by reading code: `verify()` raised `serve.sh <role>` with an
    inherited env and no explicit state, writing into the REAL `.pair/state.json`
    of the project install.sh launched from, inheriting `TMUX_PANE`. The run went
    from the agy window, and the `opencode` roster pane rewrote to the `agy` pane
    address.

    Before the session-address move that was one-run corruption. Past it the
    corruption stood firm: the role took a FOREIGN pane by a canonical address —
    one that never renumbers — and the very next `pair_deliver` would type into
    a foreign composer. Plus the roster took the check-process pid, dying at
    once, and the role read "dead at a live window".

    The test sets the same terms: launching install under a pane, watching the
    real `.pair/state.json` untouched. The pane takes REAL, never invented, for
    two reasons. The bug surfaced exactly with a live pane in env, and an
    invented pane would reproduce another. And more important, the installer
    also runs a test suite under the same env, while some tests work with the
    suite live pane: substituting a missing one would drop them, and a red
    result would speak of the substitution, not the bug.

    No mock fits here: the same code needs a real launch.
    """

    def live_pane(self) -> str:
        """The real pane the suite launched from, or a skip."""
        pane = os.environ.get("TMUX_PANE")
        if not pane:
            self.skipTest("the suite runs outside tmux")
        tmux = shutil.which("tmux")
        if not tmux:
            self.skipTest("tmux not installed")
        probe = subprocess.run([tmux, "display-message", "-p", "-t", pane, "#{pane_id}"],
                               capture_output=True, text=True, timeout=10)
        if probe.returncode != 0 or not probe.stdout.strip():
            self.skipTest(f"the suite pane {pane} never answers")
        return pane

    def test_install_from_a_pane_does_not_touch_the_real_roster(self) -> None:
        pane = self.live_pane()
        state = ROOT / ".pair" / "state.json"
        before = state.read_text(encoding="utf-8") if state.exists() else None
        env = dict(os.environ, HOME=str(self.home), TMUX_PANE=pane)
        subprocess.run([sys.executable, str(INSTALL), "opencode"],
                       capture_output=True, text=True, env=env,
                       cwd=str(ROOT), timeout=300)
        after = state.read_text(encoding="utf-8") if state.exists() else None
        # The leak the critic opencode found past round 47: comparing the FULL live
        # state.json before/after reddened on any live pair — pair processes write the
        # file during install (messages, pilot rings, evidence), so the test deterministically
        # reddened on the WITNESS, not an installer bug. What checks is what the installer
        # can truly break: the launch pane owning and the role count.
        # truly break: the launch pane owning and the role count.
        # launch and the role count.
        if before is not None and after is not None:
            self.assertEqual(
                _roles_claiming(before, pane), _roles_claiming(after, pane),
                "the launching-process pane must never move to another "
                "role: the installer added or took it in the live roster")
            # Not "opencode" for the allowlist: comparing runs on a role that no live one
            # holds. A live role appears alone (its agent writes into state during install —
            # the pair suite lives), a dead one appears by no hand but the installer.
            # holds. A live role appears alone (its agent writes into state during install —
            self.assertEqual(
                _dead_roles(before), _dead_roles(after),
                "the installer must never add or drop live-roster roles: "
                "a dead role appears or vanishes only by its hand "
                "(verify, writing into the real state.json — the 31st regression)")
        return

    def test_a_pane_may_belong_to_a_role_without_being_rejected(self) -> None:
        """The suite pane may lawfully stand in the roster.

        The suite launches from the pair own pane, and then `%0` is the writer pane.
        The old check demanded no such pane in the roster — demanding the installer
        drop a foreign record, falling on right behaviour. This test falls if anyone
        returns that wording as "the suite pane must miss from the roster" — guarding
        the guard.
        """
        pane = self.live_pane()
        state = ROOT / ".pair" / "state.json"
        if not state.exists():
            self.skipTest("no pair state")
        owners = _roles_claiming(state.read_text(encoding="utf-8"), pane)
        if not owners:
            self.skipTest("the suite pane misses from the roster: nothing to check")
        for role in owners:
            self.assertIn(role, json.loads(state.read_text(encoding="utf-8"))["roles"])

    def test_the_role_in_the_install_still_gets_registered(self) -> None:
        """The check isolation never turned "the server never launches".

        The transport check exists for one answer: the server answers as pair-mailbox.
        Had state isolation broken it, install would silently stop checking anything,
        reading as success.
        """
        result = run_install(self.home, "opencode")
        self.assertIn("check: OK, pair-mailbox", result.stdout)


class TestPaneRoleLookup(InstallCase):
    """`pane_role_pid` finds the role server, not the pane process.

    This breakage passed my own item 2 unnoticed: the tests mocked `pane_pid` with
    a live value, checking the wrong thing. On live panes `#{pane_pid}` returns
    bash, while the agent lives its child, so "someone sits in the pane" always
    reads true — and a fallen role declared forever alive, cancelling the restart
    whole.

    The real `/proc` with this suite real processes stands here, because the stub
    was exactly why the bug stayed unseen.
    """

    def test_a_role_server_is_found_among_the_descendants(self) -> None:
        # A "pane" is the suite process, its children what lives in it.
        # A check with no decoy would stand empty: with a single child the "first child"
        # and the "role server" coincide, and swapping one for the other passed unseen.
        # So a foreign process launches first, and the answer must NOT be it.
        # must NOT be it.
        decoy = subprocess.Popen(["sleep", "30"])
        self.addCleanup(decoy.kill)
        # A second foreign process, holding the role name in its args — so an agent
        # launch reads. Substring search is banned: such a process satisfies the substring
        # yet never serves as the role server, while the bug would cost a foreign pid in
        # a foreign record.
        namesake = subprocess.Popen(["sleep", "30", "probe-role"])
        self.addCleanup(namesake.kill)
        child = subprocess.Popen(
            [sys.executable, str(ROOT / "mcp/pair_mailbox/server.py"),
             "--role", "probe-role", "--state", str(Path(tempfile.mkdtemp()) / ".pair")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.addCleanup(child.kill)
        # The search takes a fixed child order. The real `/proc` walk order is arbitrary,
        # and on an arbitrary order these checks blinked: now catching the "find the role
        # server" for "return the first child" swap, now not.
        # A test whose result depends on a dir order checks luck, not code. The order sets
        # explicit here, so the fall reads deterministic.
        real_descendants = composer_module()._descendants
        composer_module()._descendants = lambda root: [decoy.pid, namesake.pid, child.pid]
        try:
            found = 0
            for _ in range(50):
                found = composer_module().find_role_pid(os.getpid(), "probe-role")
                if found:
                    break
                time.sleep(0.05)
        finally:
            composer_module()._descendants = real_descendants
        self.assertEqual(child.pid, found,
                         "the role server must find, not the first comer "
                         "a process, nor one with the role name in args")

    def test_a_process_with_the_role_name_in_its_args_is_not_the_role(self) -> None:
        """Role matching is strict: the file name and neighbour args, not a substring.

        A deterministic check instead of "some child found the wrong one": `/proc`
        order is arbitrary, and on it swapping exact for substring caught only at
        times. Here the reverse — one concrete process, one question to it.
        """
        namesake = subprocess.Popen(["sleep", "30", "probe-role"])
        self.addCleanup(namesake.kill)
        time.sleep(0.1)
        composer = composer_module()
        self.assertFalse(composer._is_role_server(namesake.pid, "probe-role"),
                         "a process with the role name in args is no role server")

    def test_a_foreign_role_is_not_claimed(self) -> None:
        """One role per pane, and a foreign role must never find as ours.

        A one-way bug — always. Finding a foreign process and writing it into a
        foreign record means wiping the difference between two roles.
        """
        found = _find_in_tree(os.getpid(), "definitely-not-a-real-role")
        self.assertEqual(0, found, "a missing role must never find")

    def test_an_empty_role_never_matches(self) -> None:
        self.assertEqual(0, _find_in_tree(os.getpid(), ""),
                         "an empty role must match nothing")

    def test_the_lookup_needs_no_tmux_at_all(self) -> None:
        """The search walks `/proc`, not tmux.

        The critic note was about the second tmux call for a pid. Past this fix none
        remains: pane facts come from `pane_identity` in one query, while the process
        search walks `/proc`. What checks is not call count but that a tmux stub can
        never block finding the role — or the "no query needed" claim would stay
        words.
        """
        real_run = subprocess.run

        def refuse_tmux(argv, **kwargs):
            if isinstance(argv, (list, tuple)) and "tmux" in " ".join(map(str, argv)):
                raise AssertionError("role search must never reach tmux")
            return real_run(argv, **kwargs)

        subprocess.run = refuse_tmux
        try:
            self.assertEqual(0, _find_in_tree(os.getpid(), "definitely-not-a-real-role"))
        finally:
            subprocess.run = real_run


def composer_module():
    """The real composer module, not its test copy.

    The search used to duplicate right here, and the test checked the copy: a prod
    fix for "return the first child" left the suite green. A test that cannot see
    a prod fix checks nothing.
    """
    sys.path.insert(0, str(ROOT / "mcp" / "pair_mailbox"))
    import composer  # noqa: PLC0415
    return composer


def _find_in_tree(root: int, role: str) -> int:
    return composer_module().find_role_pid(root, role)


class TestPilotLaunchMap(InstallCase):
    """`launch` serves the fallen-agent restart: with no it the supervisor knows the
    role is dead, but never which command raises it."""

    def test_every_profile_carries_a_launch_command(self) -> None:
        profiles = json.loads((ROOT / "cli-profiles.json").read_text(encoding="utf-8"))
        for name, profile in profiles.items():
            if name.startswith("_"):
                continue
            with self.subTest(profile=name):
                self.assertTrue(profile.get("launch"),
                                f"the profile {name} owns no restart command")

    def test_opencode_launch_is_standalone(self) -> None:
        """opencode by default shares a background daemon across windows, and two instances
        would take one config — one role for two."""
        profiles = json.loads((ROOT / "cli-profiles.json").read_text(encoding="utf-8"))
        self.assertIn("--standalone", profiles["opencode"]["launch"])

    def test_opencode_launch_paths_agree_on_auto(self) -> None:
        """Both launch paths carry --auto: or they diverge and either halts on a prompt at
        every routine run (round 33, P0).

        cli-profiles.json means the pilot restart, bin/pair-duo the live launch.
        """
        profiles = json.loads((ROOT / "cli-profiles.json").read_text(encoding="utf-8"))
        self.assertIn("--auto", profiles["opencode"]["launch"])
        duo = (ROOT / "bin" / "pair-duo").read_text(encoding="utf-8")
        self.assertIn("opencode --standalone --auto", duo)


    def test_launch_is_json_parseable(self) -> None:
        """The file also reads with --check, where any JSON error breaks install whole."""
        data = json.loads((ROOT / "cli-profiles.json").read_text(encoding="utf-8"))
        self.assertIn("_readme", data)

    def test_profiles_sharing_one_file_use_different_server_keys(self) -> None:
        """Two CLIs on one mcp.json must write different keys (#omp).

        Measured 2026-10-06: `install.sh omp` overwrote the `pair` key in
        `~/.pi/agent/mcp.json` with the `omp` role, silently breaking MCP for `pi`.
        A shared file with no key split means one role for two.
        """
        profiles = json.loads((ROOT / "cli-profiles.json").read_text(encoding="utf-8"))
        by_path: dict[str, dict[str, str]] = {}
        for name, profile in profiles.items():
            if name.startswith("_"):
                continue
            spec = (profile or {}).get("mcp") or {}
            by_path.setdefault(str(spec.get("path")), {})[name] = str(
                spec.get("server_key", "pair"))
        for path, keys in by_path.items():
            with self.subTest(path=path):
                self.assertEqual(len(keys), len(set(keys.values())),
                                 f"one file {path} — different keys: {keys}")

    def test_no_two_profiles_share_one_config_home(self) -> None:
        """Two DIFFERENT engines on one config home mean one shared role (ROLE-BUG
        2026-10-07).

        Measured 2026-10-07: the `omp` binary symlinks pi-coding-agent, but its home
        is `~/.omp/agent/`, while the `omp` profile wrote into `~/.pi/agent/mcp.json`.
        Worse: `omp`/`pi` load foreign MCPs over discovery (OpenCode/Claude/Codex/
        Cursor/Gemini/VSCode/Windsurf), and the `pair` key collides across sources —
        the `omp` process raised `server.py --role opencode` from the project
        `.opencode/opencode.jsonc`, losing write authority. A profile must point at
        its own engine home, never a foreign one.
        """
        profiles = json.loads((ROOT / "cli-profiles.json").read_text(encoding="utf-8"))
        home_of = {"pi": ".pi", "omp": ".omp", "opencode": ".config/opencode",
                   "agy": ".gemini", "codex": ".codex", "claude": ".claude"}
        for name, profile in profiles.items():
            if name.startswith("_"):
                continue
            spec = (profile or {}).get("mcp") or {}
            path = str(spec.get("path"))
            with self.subTest(profile=name):
                if name in home_of:
                    self.assertIn(home_of[name], path,
                                 f"profile {name} writes into a foreign home: {path}")


if __name__ == "__main__":
    unittest.main(verbosity=2)