"""Bootstrap tests for bin/pairup: the anti-mock.json template for new projects.

Runs the bootstrap file fragment in an isolated tmp git project: the full
pairup drags pilot/CLI and is not hermetic. Checks the R84 contract: the
template copies on creation, an existing one is never overwritten.
"""
from __future__ import annotations

import json
import os
import hashlib
import time
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAIRUP = ROOT / "bin" / "pairup"


def process_environment(pid: int) -> list[str]:
    return subprocess.run(["ps", "eww", "-p", str(pid)], capture_output=True,
                          text=True, check=True).stdout.split()

def script_command(command: str) -> list[str]:
    if sys.platform == "darwin":
        return ["script", "-q", "/dev/null", "sh", "-c", command]
    return ["script", "-e", "-q", "-c", command, "/dev/null"]

BOOTSTRAP = """\
_root=%s
PAIRUP_ROOT=%s
mkdir -p "$_root/.pair/mailbox"
%s
"""


def _snippet() -> str:
    """Template-copy and engine-sanction lines from bin/pairup — the test rides
    the code.

    Anchored on structure, not prose: a bare `fi` substring also matches
    inside `if`, and a comment wording breaks on every rephrase. The mkdir
    line is what the template tests depend on, and the block runs through
    `STATE_DIR="$_root/.pair"` so the engine-sanction heredocs that follow it
    execute too — they are the round-120 contract, and a snippet that stopped
    at the first `fi` silently ran none of them.
    """
    text = PAIRUP.read_text(encoding="utf-8")
    start = text.index('mkdir -p "$_root/.pair/mailbox"')
    end = text.index('STATE_DIR="$_root/.pair"', start)
    return text[start:end]


class PairupLaunchCase(unittest.TestCase):
    """Exercise the real entry point with local stand-ins, never the user's tmux."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.project = base / "project's space"
        self.project.mkdir()
        self.home = base / "home"
        self.home.mkdir()
        self.bin = base / "bin"
        self.bin.mkdir()
        self.log = base / "tmux.jsonl"
        self.marker = base / "session-exists"
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        for name in ("codex", "agy", "pgrep"):
            path = self.bin / name
            path.write_text("#!/bin/sh\nexit 0\n")
            path.chmod(0o755)
        tmux = self.bin / "tmux"
        tmux.write_text("#!" + sys.executable + "\n" +
            "import json, os, sys\nfrom pathlib import Path\n" +
            "args=sys.argv[1:]\n" +
            "with open(os.environ['PAIR_TEST_LOG'], 'a') as log: log.write(json.dumps(args)+'\\n')\n" +
            "if args[:1]==['-L']: args=args[2:]\n" +
            "marker=Path(os.environ['PAIR_TEST_MARKER'])\n" +
            "if args[:1]==['has-session']: sys.exit(0 if marker.exists() else 1)\n" +
            "if args[:1]==['new-session']: marker.touch()\n")
        tmux.chmod(0o755)
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith("PAIR_") and k not in ("TMUX", "TMUX_PANE")}
        self.env.update(HOME=str(self.home), PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
                        PAIR_TEST_LOG=str(self.log), PAIR_TEST_MARKER=str(self.marker))

    def launch(self, *args):
        return subprocess.run(["bash", str(PAIRUP), "--in-place", *args], cwd=self.project,
                              env=self.env, capture_output=True, text=True, timeout=30)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_fresh_launch_records_literal_task_and_second_launch_attaches(self):
        task = 'Inspect "auth"; $(touch SHOULD_NOT_EXIST) — проверка'
        first = self.launch(task)
        self.assertEqual(first.returncode, 0, first.stderr + first.stdout)
        state = json.loads((self.project / ".pair/state.json").read_text())
        self.assertEqual(state["writer"], "codex")
        self.assertEqual(state["tasks"][0]["text"], task)
        second = self.launch()
        self.assertEqual(second.returncode, 0, second.stderr + second.stdout)
        calls = self.calls()
        self.assertEqual(sum("new-session" in call for call in calls), 1)
        self.assertEqual(sum("split-window" in call for call in calls), 1)
        self.assertFalse((self.project / "SHOULD_NOT_EXIST").exists())
        created = next(call for call in calls if "new-session" in call)
        self.assertEqual(created[0], "-L", "use the project's socket consistently")
        self.assertIn("launch_context.py", created[-1])
        executed = subprocess.run(["sh", "-c", created[-1]], cwd=self.project,
                                  env=self.env, text=True, capture_output=True, timeout=10)
        self.assertEqual(executed.returncode, 0, executed.stderr)

    def test_launch_inside_foreign_tmux_creates_its_own_session(self):
        self.env["TMUX"] = "/tmp/foreign-tmux,123,0"
        result = self.launch("--task", "inspect")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        split = next(call for call in self.calls() if "split-window" in call)
        self.assertEqual(split[0], "-L")
        self.assertIn("-t", split)

    def test_missing_custom_state_is_rejected_without_creating_a_different_room(self):
        target = Path(self.tmp.name) / "custom-state"
        result = self.launch("--state", str(target))
        self.assertEqual(result.returncode, 2)
        self.assertFalse(target.exists())
        self.assertFalse((target.parent / ".pair").exists())


class PairupEngineSanctionCase(unittest.TestCase):
    """The multi-project contract of round 120: a target project with no package
    of its own records WHICH pair-mailbox tree serves it, and the record is
    refreshed when the engine tree moves. Without it serve.sh's origin guard
    refuses every multi-project launch (measured on galatour, 2026-10-18).
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="pairup-engine-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.proj = self.tmp / "proj"
        self.proj.mkdir()
        subprocess.run(["git", "init", "-q", str(self.proj)], check=True)

    def _bootstrap(self, engine_root: Path) -> None:
        script = BOOTSTRAP % (self.proj, engine_root, _snippet())
        proc = subprocess.run(["bash", "-c", script], capture_output=True,
                              text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def _state(self) -> dict:
        return json.loads((self.proj / ".pair" / "state.json").read_text(encoding="utf-8"))

    def test_new_state_records_the_engine_root(self) -> None:
        engine = self.tmp / "engine"
        engine.mkdir()
        self._bootstrap(engine)
        self.assertEqual((self.proj / ".pair" / "engine").read_text(encoding="utf-8"),
                         str(engine))
        self.assertEqual(self._state()["engine_root"], str(engine))

    def test_moved_engine_refreshes_the_sanction(self) -> None:
        first = self.tmp / "engine-one"
        first.mkdir()
        self._bootstrap(first)
        second = self.tmp / "engine-two"
        second.mkdir()
        self._bootstrap(second)
        self.assertEqual((self.proj / ".pair" / "engine").read_text(encoding="utf-8"),
                         str(second))
        self.assertEqual(self._state()["engine_root"], str(second),
                         "state.json and .pair/engine must agree, or one is stale")

    def test_unmoved_engine_changes_nothing(self) -> None:
        engine = self.tmp / "engine"
        engine.mkdir()
        self._bootstrap(engine)
        before = (self.proj / ".pair" / "state.json").read_text(encoding="utf-8")
        self._bootstrap(engine)
        self.assertEqual((self.proj / ".pair" / "state.json").read_text(encoding="utf-8"),
                         before, "a second pairup on an unchanged engine must be a no-op")


class PairupTemplateCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="pairup-template-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.dist = self.tmp / "dist"
        self.dist.mkdir()
        (self.dist / "anti-mock.json").write_text(
            (ROOT / "anti-mock.json").read_text(encoding="utf-8"),
            encoding="utf-8")
        self.proj = self.tmp / "proj"
        self.proj.mkdir()
        subprocess.run(["git", "init", "-q", str(self.proj)], check=True)

    def run_bootstrap(self) -> None:
        script = BOOTSTRAP % (self.proj, self.dist, _snippet())
        proc = subprocess.run(["bash", "-c", script], capture_output=True,
                              text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_template_copied_to_new_project(self) -> None:
        self.run_bootstrap()
        target = self.proj / "anti-mock.json"
        self.assertTrue(target.is_file())
        self.assertEqual(
            target.read_text(encoding="utf-8"),
            (ROOT / "anti-mock.json").read_text(encoding="utf-8"))

    def test_existing_template_not_clobbered(self) -> None:
        target = self.proj / "anti-mock.json"
        target.write_text('{"patterns": []}', encoding="utf-8")
        self.run_bootstrap()
        self.assertEqual(target.read_text(encoding="utf-8"),
                         '{"patterns": []}')


class PairupStateOwnershipCase(unittest.TestCase):
    """Round 121: the project root owns the state dir, never the shell's env.

    Measured live: `tmux new -s nyx` inside another project's session, then
    `pairup` — both panes served the foreign galatour room (`ps` showed
    --state .../galatour/.pair from both), while .../nyx-system/.pair was served
    by no live server. The old code took PAIR_STATE on its face
    (`STATE_DIR="$PAIR_STATE"`), so a stale shell repointed the whole launch.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="pairup-own-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.proj = self.tmp / "proj"
        self.proj.mkdir()
        subprocess.run(["git", "init", "-q", str(self.proj)], check=True)
        (self.proj / ".pair" / "mailbox").mkdir(parents=True)
        (self.proj / ".pair" / "state.json").write_text(
            json.dumps({"round": 1, "phase": "implement"}), encoding="utf-8")

    def _resolve(self, env_state: str | None) -> subprocess.CompletedProcess:
        """Run only the state-resolution head of pairup: the part that decides
        which room this launch belongs to. It exits before the pilot, so the
        probe is hermetic — no roles, no tmux, no daemon."""
        # Anchors bracket the resolution head of pairup — the part that decides
        # which room this launch belongs to. The head ends at `usage()`, and the
        # rule under test is the block that starts at HERE_STATE (round 121's
        # rewrite: the root is resolved first and the env is judged against it).
        text = PAIRUP.read_text(encoding="utf-8")
        start = text.index('HERE_STATE=""')
        end = text.index('usage() {', start)
        script = "PAIRUP_ROOT=%s\n%s\n" % (ROOT, text[start:end])
        env = dict(__import__("os").environ)
        env["PAIRUP_ROOT"] = str(ROOT)
        env.pop("PAIR_STATE", None)
        env.pop("PAIR_TRUST_ENV", None)
        env.pop("TMUX", None)
        if env_state is not None:
            env["PAIR_STATE"] = env_state
        return subprocess.run(["sh", "-c", script + '\nprintf "%s" "$STATE_DIR"'],
                              capture_output=True, text=True, env=env, timeout=60,
                              cwd=str(self.proj))

    def test_own_project_wins_over_a_foreign_inherited_room(self) -> None:
        other = self.tmp / "other"
        (other / ".pair").mkdir(parents=True)
        proc = self._resolve(str(other / ".pair"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), str(self.proj / ".pair"),
                         "the root's own .pair is authoritative")

    def test_explicit_trust_env_keeps_the_room_it_names(self) -> None:
        """The escape hatch stays: an operator who KNOWS which room they want can
        say so. Refusing every non-matching env would break deliberate use."""
        other = self.tmp / "other"
        (other / ".pair").mkdir(parents=True)
        env = dict(__import__("os").environ)
        env["PAIRUP_ROOT"] = str(ROOT)
        env.pop("PAIR_STATE", None)
        env.pop("TMUX", None)
        env["PAIR_TRUST_ENV"] = "1"
        env["PAIR_STATE"] = str(other / ".pair")
        proc = subprocess.run(
            [str(ROOT / "mcp" / "pair_mailbox" / "state_resolve.sh")],
            capture_output=True, text=True, env=env, timeout=60,
            cwd=str(self.proj))
        self.assertEqual(proc.stdout.splitlines()[0].strip(), str(other / ".pair"),
                         "PAIR_TRUST_ENV=1 is the operator's deliberate override")


class PairupWorktreeDefaultCase(unittest.TestCase):
    """Round 153: the worktree is the DEFAULT, and `--in-place` opts out.

    The launch environment of a pair is an isolated checkout: a dirty main
    worktree must not swallow the round, and two launches must not share one
    tree. The opt-out is explicit — a silent default would be a surprise to
    the operator who expects the pair in the checkout they are standing in.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="pairup-wt-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.proj = self.tmp / "proj"
        self.proj.mkdir()
        subprocess.run(["git", "init", "-q", str(self.proj)], check=True)
        subprocess.run(["git", "config", "user.email", "p@t.t"], cwd=self.proj, check=True)
        subprocess.run(["git", "config", "user.name", "P"], cwd=self.proj, check=True)
        (self.proj / "a.txt").write_text("x\n", encoding="utf-8")
        subprocess.run(["git", "add", "a.txt"], cwd=str(self.proj), check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=str(self.proj), check=True)

    def _block(self) -> str:
        """The launch-isolation block of pairup: the part that decides
        whether the pair runs in a worktree or in this checkout.

        The block itself contains a nested `if [ -z "$STATE_DIR" ]`, so the
        end anchor is the OUTER one — the one that opens the "no state" case
        and carries the comment that belongs to it.
        """
        text = PAIRUP.read_text(encoding="utf-8")
        start = text.index("# The worktree is the DEFAULT (round 153)")
        end = text.index('if [ -z "$STATE_DIR" ]; then\n  # No state', start)
        return text[start:end]

    def _run(self, args: str = "", in_place: bool = False) -> subprocess.CompletedProcess:
        """Run the launch-isolation block with the flags the parser would have
        set. `in_place` sets the variable BEFORE the block, exactly where the
        flag parser leaves it — setting it after would test nothing."""
        env = dict(__import__("os").environ)
        env["PAIRUP_ROOT"] = str(ROOT)
        env.pop("PAIR_STATE", None)
        env.pop("PAIR_TRUST_ENV", None)
        env.pop("TMUX", None)
        prefix = "IN_PLACE=1\n" if in_place else ""
        script = "PAIRUP_ROOT=%s\n%s%s\nprintf 'CWD=%%s' \"$PWD\"\n" % (
            ROOT, prefix, self._block())
        return subprocess.run(["bash", "-c", script + args], capture_output=True,
                              text=True, env=env, timeout=120, cwd=str(self.proj))

    def test_without_a_flag_the_pair_lands_in_a_worktree(self) -> None:
        proc = self._run("")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("switching to worktree", proc.stdout)
        cwd = proc.stdout.split("CWD=")[-1].strip()
        self.assertNotEqual(cwd, str(self.proj),
                            "the default launch must leave the checkout it was run in")
        self.assertIn(".worktrees", cwd)

    def test_the_default_worktree_is_reused_not_duplicated(self) -> None:
        """The name is derived from the project path, so a second launch lands
        in the SAME worktree instead of creating another one."""
        first = self._run("")
        second = self._run("")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(first.stdout.split("CWD=")[-1].strip(),
                         second.stdout.split("CWD=")[-1].strip())

    def test_in_place_stays_in_the_checkout(self) -> None:
        """The opt-out is explicit and total: no worktree, no switch."""
        in_place = self._run(in_place=True)
        self.assertEqual(in_place.returncode, 0, in_place.stderr)
        self.assertNotIn("switching to worktree", in_place.stdout)
        self.assertEqual(Path(in_place.stdout.split("CWD=")[-1].strip()).resolve(), self.proj.resolve())

    def test_outside_a_git_repository_the_launch_refuses_with_the_way_out(self) -> None:
        plain = self.tmp / "plain"
        plain.mkdir()
        env = dict(__import__("os").environ)
        env["PAIRUP_ROOT"] = str(ROOT)
        env.pop("PAIR_STATE", None)
        env.pop("PAIR_TRUST_ENV", None)
        script = "PAIRUP_ROOT=%s\n%s\n" % (ROOT, self._block())
        proc = subprocess.run(["bash", "-c", script], capture_output=True,
                              text=True, env=env, timeout=60, cwd=str(plain))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("--in-place", proc.stderr)

    def test_the_flag_table_names_the_default_and_the_opt_out(self) -> None:
        text = PAIRUP.read_text(encoding="utf-8")
        self.assertIn("--in-place", text)
        self.assertIn("--worktree", text)
        proc = subprocess.run([str(PAIRUP), "--help"], capture_output=True,
                              text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("--in-place", proc.stdout)
        self.assertIn("(the DEFAULT)", proc.stdout)

    def test_the_panes_work_in_the_worktree_and_the_state_stays_in_the_project(self) -> None:
        """Two directories, two jobs. The agents EDIT the worktree; the gates
        READ the state, and the state belongs to the project that owns it.

        Conflating them is what made `--worktree` useless: the launch cd'ed
        into the worktree and the panes cd'ed straight back out of it.
        """
        text = PAIRUP.read_text(encoding="utf-8")
        self.assertIn("LAUNCH_DIR", text)
        # The pane init strings and the tmux calls must all name the launch
        # dir, never the project dir.
        for line in text.splitlines():
            if "init=" in line or "tmux " in line:
                if "PROJECT_DIR" in line and "LAUNCH_DIR" not in line:
                    # The socket hash is derived from the project dir on
                    # purpose: the server is per PROJECT, not per tree.
                    if "sha256sum" in line or "cksum" in line or "_RESOLVED" in line:
                        continue
                    self.fail(f"the panes must work in the launch dir: {line.strip()}")
        self.assertTrue('-c "$LAUNCH_DIR" "$writer_init"' in text,
                        "the writer pane must start in the worktree directory")


class PairupTmuxLaunchCase(unittest.TestCase):
    """Run the complete launcher against real isolated tmux servers.

    CLI fixtures stay alive in their panes; these tests exercise launching,
    terminal attachment and isolation, not model inference or authentication.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="pairup-launch-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.project = self.base / "project"
        self.project.mkdir()
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        self.home = self.base / "home"
        self.home.mkdir()
        self.commands = self.base / "bin"
        self.commands.mkdir()
        for cli in ("codex", "agy", "claude"):
            command = self.commands / cli
            command.write_text("#!/bin/sh\nprintf '%s\\n' \"$PWD|$PAIR_STATE|" + cli + "\" > \"$PWD/" + cli + ".started\"\nexec sleep 120\n")
            command.chmod(0o755)
        self.env = dict(os.environ)
        self.env.update(HOME=str(self.home), PATH=str(self.commands) + os.pathsep + self.env["PATH"], TERM="xterm-256color")
        for key in ("PAIR_STATE", "PAIR_ROLE", "PAIR_TRUST_ENV", "TMUX", "PAIR_TMUX_SOCKET", "PAIR_FORCE_SOCKET", "PAIR_PANE_SOCKET"):
            self.env.pop(key, None)
        digest = hashlib.sha256(str(self.project.resolve()).encode()).hexdigest()[:8]
        self.socket = "pair-project-" + digest
        self.foreign = "pair-test-foreign-" + digest
        self.procs = []
        self.addCleanup(self.cleanup)

    def tmux(self, *args: str, foreign: bool = False) -> subprocess.CompletedProcess:
        return subprocess.run(["tmux", "-L", self.foreign if foreign else self.socket, *args],
                              capture_output=True, text=True, timeout=10)

    def cleanup(self) -> None:
        for socket in (self.socket, self.foreign):
            subprocess.run(["tmux", "-L", socket, "kill-server"], capture_output=True)
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
            proc.communicate(timeout=10)
        pidfile = self.project / ".pair" / "pilot.pid"
        if pidfile.exists():
            try:
                os.kill(int(pidfile.read_text()), 15)
            except ProcessLookupError:
                pass

    def launch(self, critic: str = "agy", foreign: bool = False) -> subprocess.Popen:
        env = dict(self.env)
        if foreign:
            probe = self.tmux("display-message", "-p", "-t", "foreign", "#{socket_path}", foreign=True)
            self.assertEqual(probe.returncode, 0, probe.stderr)
            env["TMUX"] = probe.stdout.strip() + ",1,0"
        command = shlex.join([str(PAIRUP), "--in-place", "--writer", "codex", "--swap", critic])
        proc = subprocess.Popen(script_command(command), cwd=self.project,
                                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.procs.append(proc)
        return proc

    def await_launch(self, proc: subprocess.Popen, critic: str) -> None:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            clients = self.tmux("list-clients", "-F", "#{client_pid}")
            if (self.project / (critic + ".started")).exists() and clients.stdout.strip():
                return
            if proc.poll() is not None:
                self.fail("launcher exited: " + str(proc.communicate()))
            time.sleep(.1)
        self.fail("pair did not start with attached SSH-like terminal")

    def detach(self, proc: subprocess.Popen) -> None:
        result = self.tmux("detach-client", "-a", "-s", self.socket)
        self.assertEqual(result.returncode, 0, result.stderr)
        out, err = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 0, out + err)

    def test_fresh_ssh_launch_reattach_and_replace_critic(self) -> None:
        first = self.launch()
        self.await_launch(first, "agy")
        panes = self.tmux("list-panes", "-a", "-F", "#{pane_id}|#{pane_pid}|#{pane_title}").stdout
        self.assertEqual(len(panes.splitlines()), 2, panes)
        pilot_pid = int((self.project / ".pair" / "pilot.pid").read_text())
        pilot_env = process_environment(pilot_pid)
        self.assertIn("PAIR_PANE_SOCKET=" + self.socket, pilot_env)

        writer = [line for line in panes.splitlines() if "(writer)" in line][0]
        self.detach(first)
        second = self.launch()
        self.await_launch(second, "agy")
        self.assertEqual(self.tmux("list-panes", "-a", "-F", "#{pane_id}|#{pane_pid}|#{pane_title}").stdout, panes)
        self.detach(second)
        third = self.launch("claude")
        self.await_launch(third, "claude")
        after = self.tmux("list-panes", "-a", "-F", "#{pane_id}|#{pane_pid}|#{pane_title}").stdout
        self.assertEqual(len(after.splitlines()), 2, after)
        self.assertIn(writer, after)
        self.assertIn("project:claude (critic)", after)
        self.detach(third)

    def test_live_writer_change_refuses_without_changing_authority(self) -> None:
        proc = self.launch()
        self.await_launch(proc, "agy")
        self.detach(proc)
        before = self.tmux("list-panes", "-a", "-F", "#{pane_id}|#{pane_pid}").stdout
        state = json.loads((self.project / ".pair" / "state.json").read_text())
        command = shlex.join([str(PAIRUP), "--in-place", "--writer", "agy", "--swap", "codex"])
        result = subprocess.run(script_command(command),
                                cwd=self.project, env=self.env, capture_output=True,
                                text=True, timeout=20)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("authority left unchanged", result.stdout)
        after = json.loads((self.project / ".pair" / "state.json").read_text())
        self.assertEqual(after.get("writer"), state.get("writer"))
        self.assertEqual(after.get("policy"), state.get("policy"))
        self.assertEqual(self.tmux("list-panes", "-a", "-F", "#{pane_id}|#{pane_pid}").stdout, before)

    def test_old_supervisor_is_restarted_on_the_project_socket(self) -> None:
        first = self.launch()
        self.await_launch(first, "agy")
        self.detach(first)
        pidfile = self.project / ".pair" / "pilot.pid"
        old_pid = int(pidfile.read_text())
        os.kill(old_pid, 15)
        env = dict(self.env, PAIR_PANE_SOCKET="foreign-test-socket")
        stale = subprocess.Popen(["python3", str(ROOT / "bin" / "pairpilot"),
                                  "--state", str(self.project / ".pair")],
                                 env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.procs.append(stale)
        pidfile.write_text(str(stale.pid))
        second = self.launch()
        self.await_launch(second, "agy")
        new_pid = int(pidfile.read_text())
        self.assertNotEqual(new_pid, stale.pid)
        self.assertIn("PAIR_PANE_SOCKET=" + self.socket,
                      process_environment(new_pid))
        self.detach(second)

    def test_unstoppable_supervisor_does_not_spawn_a_second_instance(self) -> None:
        first = self.launch()
        self.await_launch(first, "agy")
        self.detach(first)
        pidfile = self.project / ".pair" / "pilot.pid"
        os.kill(int(pidfile.read_text()), 15)
        fixture = self.commands / "pairpilot"
        ready = self.base / "ready"
        fixture.write_text("import signal,time\nfrom pathlib import Path\n"
                           "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                           + f"Path({str(ready)!r}).touch()\n" + "time.sleep(120)\n")
        stale = subprocess.Popen(["python3", str(fixture), "--state", str(self.project / ".pair")],
                                 env=dict(self.env, PAIR_PANE_SOCKET="old-socket"))
        self.procs.append(stale)
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertTrue(ready.exists())
        pidfile.write_text(str(stale.pid))
        command = shlex.join([str(PAIRUP), "--in-place", "--writer", "codex", "--swap", "agy"])
        result = subprocess.run(script_command(command),
                                cwd=self.project, env=self.env, capture_output=True,
                                text=True, timeout=15)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing to start a second instance", result.stdout)
        self.assertEqual(int(pidfile.read_text()), stale.pid)
        self.assertIsNone(stale.poll())

    def test_foreign_tmux_is_not_split_or_reused(self) -> None:
        foreign = self.tmux("new-session", "-d", "-s", "foreign", "sleep 120", foreign=True)
        self.assertEqual(foreign.returncode, 0, foreign.stderr)
        before = self.tmux("list-panes", "-a", "-F", "#{pane_id}|#{pane_pid}", foreign=True).stdout
        proc = self.launch(foreign=True)
        self.await_launch(proc, "agy")
        self.assertEqual(self.tmux("list-panes", "-a", "-F", "#{pane_id}|#{pane_pid}", foreign=True).stdout, before)
        self.assertEqual(len(self.tmux("list-panes", "-a").stdout.splitlines()), 2)
        self.detach(proc)


if __name__ == "__main__":
    unittest.main(verbosity=2)
