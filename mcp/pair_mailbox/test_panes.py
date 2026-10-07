"""Тесты владения панелью: наша ли она и при каком условии доказана.

Один тест-файл на один исходник: `panes.py` отвечает за одну обязанность —
сказать, панель ли это нашего проекта, — и проверяется здесь.

Ключевое разделение, ради которого модуль и выделен: «панель чужого проекта» и
«панель мертва» — это разные факты. `pane_belongs_to_project` не спрашивает о
живости, и проверяется здесь именно это: смешать их значит объявить роль с
умершей панелью ещё и «чужой», а снятие устаревшей записи перестало бы срабатывать
там, где оно единственно и работает.

Проверки принадлежности идут на живом tmux и на настоящей панели, созданной
набором внутри временного проекта: подмена `pane_identity` проверила бы, что
сравнение строк написано правильно, а не что панель опознаётся как наша.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import composer  # noqa: E402
import panes  # noqa: E402

TMUX = "tmux"


def have_tmux() -> bool:
    from shutil import which

    return bool(which(TMUX))


class PaneOwnershipBase(unittest.TestCase):
    """Временный проект и живая панель внутри него."""

    def setUp(self) -> None:
        if not have_tmux():
            self.skipTest("tmux не установлен")
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.project = Path(self._tmp.name) / "proj"
        (self.project / ".pair").mkdir(parents=True)
        self.pane = self._split(self.project)
        if not self.pane:
            self.skipTest("tmux не дал панель")
        # Дать tmux проставить cwd: первый опрос отдаёт путь предыдущей панели.
        time.sleep(panes.PANE_CWD_SETTLE_SECONDS + 0.3)

    def _split(self, where: Path) -> str:
        made = subprocess.run(
            [TMUX, "split-window", "-d", "-P", "-F", "#{pane_id}", "-c", str(where)],
            capture_output=True, text=True, timeout=10)
        pane = made.stdout.strip()
        if pane:
            self.addCleanup(subprocess.run, [TMUX, "kill-pane", "-t", pane],
                            capture_output=True, timeout=10)
        return pane

    def other_project(self) -> Path:
        """Второй временный проект с живой панелью — «чужой» для проверки."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        other = Path(tmp.name) / "other"
        (other / ".pair").mkdir(parents=True)
        self.other_pane = self._split(other)
        time.sleep(panes.PANE_CWD_SETTLE_SECONDS + 0.3)
        return other


class TestPaneBelongsToProject(PaneOwnershipBase):
    def test_a_pane_of_this_project_is_ours(self) -> None:
        ours, why = panes.pane_belongs_to_project(TMUX, self.pane, self.project)
        self.assertTrue(ours, why)

    def test_a_pane_of_another_project_is_not_ours(self) -> None:
        other = self.other_project()
        ours, why = panes.pane_belongs_to_project(TMUX, self.other_pane, self.project)
        self.assertFalse(ours)
        self.assertIn(str(other), why)

    def test_a_panel_inside_the_project_counts_as_ours(self) -> None:
        """Панель может быть запущена из подкаталога проекта, и это всё ещё наша.

        Отвергать такой случай нельзя: агент, открывший второй терминал внутри
        репозитория, потерял бы адресацию.
        """
        nested = self.project / "sub" / "deep"
        nested.mkdir(parents=True)
        pane = self._split(nested)
        if not pane:
            self.skipTest("tmux не дал панель")
        time.sleep(panes.PANE_CWD_SETTLE_SECONDS + 0.3)
        ours, why = panes.pane_belongs_to_project(TMUX, pane, self.project)
        self.assertTrue(ours, why)

    def test_a_panel_in_the_shared_parent_is_accepted_only_when_it_is_really_ours(
        self) -> None:
        """Общий родитель вроде ~/work не делает чужую панель своей.

        Проверяется на панели ВТОРОГО проекта, а не своей: своя панель лежит в
        корне проекта, и «панель принята при корне как своя» — это не проверка на
        родителя, а проверка совпадения. Родитель проверяется тем, что панель
        соседнего проекта при корне-родителе отвергается: иначе ~/work объявил бы
        панель своей сразу нескольким проектам на этой машине.
        """
        self.other_project()
        ours, why = panes.pane_belongs_to_project(
            TMUX, self.other_pane, self.project.parent)
        self.assertFalse(ours, why)

    def test_a_phantom_pane_is_not_proven(self) -> None:
        """tmux отвечает на несуществующую панель кодом 0 и пустым выводом.

        Отсюда «панель отсутствует», а не «своя»: несуществующее не может быть
        доказанным.
        """
        ours, why = panes.pane_belongs_to_project(TMUX, "%999999", self.project)
        self.assertFalse(ours)
        self.assertIn("отсутствует", why)

    def test_an_unreported_cwd_is_not_proven(self) -> None:
        """Нет cwd — нет доказательства, и это не «своя»."""
        with mock.patch.object(panes.composer, "pane_identity",
                               return_value={"exists": True, "alive": True,
                                             "path": "", "address": "x:0.0"}):
            ours, why = panes.pane_belongs_to_project(TMUX, "x:0.0", self.project)
        self.assertFalse(ours)
        self.assertIn("cwd", why)

    def test_a_pane_that_died_is_absent_not_foreign(self) -> None:
        """Убитая панель — это ОТСУТСТВИЕ, а не «чужой проект».

        Различие содержательное: снятие устаревшей записи ростера опирается на
        «панели нет», и подмена его на «панель чужая» увёл бы снятие не туда.
        Проверяется на настоящем `kill-pane`, потому что подменённый снимок
        ничего бы не доказывал.
        """
        subprocess.run([TMUX, "kill-pane", "-t", self.pane],
                       capture_output=True, timeout=10)
        time.sleep(0.3)
        ours, why = panes.pane_belongs_to_project(TMUX, self.pane, self.project)
        self.assertFalse(ours)
        self.assertIn("отсутствует", why)

    def test_a_live_pane_is_not_mistaken_for_a_dead_one(self) -> None:
        """Обратная сторона: живая панель доказывает принадлежность, а не «нет».

        Именно этот промах стоил бы отказа в адресации только что появившемуся
        агенту.
        """
        ours, why = panes.pane_belongs_to_project(TMUX, self.pane, self.project)
        self.assertTrue(ours, why)


class TestPaneIsForeignStillRefusesToPrint(PaneOwnershipBase):
    def test_a_phantom_cannot_be_printed_to(self) -> None:
        foreign, why = panes.pane_is_foreign(TMUX, "%999999", self.project)
        self.assertTrue(foreign)
        self.assertIn("отсутствует", why)

    def test_a_pane_that_died_cannot_be_printed_to(self) -> None:
        """Противоположная сторона: печатать нельзя, даже если проект тот же.

        Принадлежность и возможность печати — разные вопросы, и оба закрыты.
        """
        subprocess.run([TMUX, "kill-pane", "-t", self.pane],
                       capture_output=True, timeout=10)
        time.sleep(0.3)
        foreign, why = panes.pane_is_foreign(TMUX, self.pane, self.project)
        self.assertTrue(foreign)
        self.assertIn("отсутствует", why)

    def test_another_projects_pane_is_refused(self) -> None:
        self.other_project()
        foreign, why = panes.pane_is_foreign(TMUX, self.other_pane, self.project)
        self.assertTrue(foreign)
        self.assertIn("другому проекту", why)

    def test_our_own_pane_is_printable(self) -> None:
        foreign, why = panes.pane_is_foreign(TMUX, self.pane, self.project)
        self.assertFalse(foreign, why)


class TestComposerDelegatesWithoutForkingBehaviour(PaneOwnershipBase):
    def test_composer_pane_is_foreign_matches(self) -> None:
        """Обёртка в `composer` обязана вести себя как `panes`.

        Иначе доставка печатала бы по одной проверке, а ростер снимал бы панель по
        другой, и расхождение проявилось бы как «доставка не работает у живого
        соседа».
        """
        for pane, root in ((self.pane, self.project),
                           ("%999999", self.project)):
            self.assertEqual(panes.pane_is_foreign(TMUX, pane, root),
                             composer.pane_is_foreign(TMUX, pane, root),
                             f"{pane}: composer и panes обязаны отвечать одинаково")

    def test_composer_belongs_matches(self) -> None:
        for pane, root in ((self.pane, self.project),
                           ("%999999", self.project)):
            self.assertEqual(panes.pane_belongs_to_project(TMUX, pane, root),
                             composer.pane_belongs_to_project(TMUX, pane, root))


class TestFindOwnPane(PaneOwnershipBase):
    def test_own_process_tree_resolves_to_a_live_pane(self) -> None:
        """Без TMUX_PANE ростер оставался без панели и deliver/unblock слепли.
        Обратный путь — цепочка предков до pane_pid: находит живую панель."""
        import os
        if not os.path.exists(f"/proc/{os.getpid()}/stat"):
            self.skipTest("нет /proc — дереву предков негде жить")
        found = composer.find_own_pane(TMUX)
        if not found:
            self.skipTest("набор запущен вне tmux — предков панели нет")
        self.assertTrue(composer.pane_alive(TMUX, found))

    def test_unknown_tmux_binary_gives_empty(self) -> None:
        """Нет tmux — честный "", а не выдуманная панель."""
        self.assertEqual(composer.find_own_pane("tmux-no-such-binary-xyz"), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)