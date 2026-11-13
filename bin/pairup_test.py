"""Bootstrap tests for bin/pairup: the anti-mock.json template for new projects.

Runs the bootstrap file fragment in an isolated tmp git project: the full
pairup drags pilot/CLI and is not hermetic. Checks the R84 contract: the
template copies on creation, an existing one is never overwritten.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAIRUP = ROOT / "bin" / "pairup"

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
        return subprocess.run(["bash", str(PAIRUP), *args], cwd=self.project,
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
