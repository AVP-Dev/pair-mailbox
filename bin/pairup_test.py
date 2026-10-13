"""Тесты bootstrap bin/pairup: шаблон anti-mock.json новому проекту.

Гоняется файловый фрагмент bootstrap в изолированном tmp git-проекте:
полный pairup тянет pilot/CLI и не hermetic. Проверяется контракт R84:
шаблон копируется при создании, существующий не затирается.
"""
from __future__ import annotations

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
    """Строки копирования шаблона из bin/pairup — тест едет за кодом."""
    text = PAIRUP.read_text(encoding="utf-8")
    start = text.index("# Ground-truth шаблон новому проекту")
    end = text.index("fi", start) + len("fi")
    return text[start:end]


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
