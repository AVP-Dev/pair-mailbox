"""Bootstrap tests for bin/pairup: the anti-mock.json template for new projects.

Runs the bootstrap file fragment in an isolated tmp git project: the full
pairup drags pilot/CLI and is not hermetic. Checks the R84 contract: the
template copies on creation, an existing one is never overwritten.
"""
from __future__ import annotations

import json
import shutil
import subprocess
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
