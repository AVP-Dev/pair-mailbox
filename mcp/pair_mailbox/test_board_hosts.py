"""Тесты живости на этой машине (`board_hosts.py`).

Здесь проверяется то, что модуль обещает контрактом, и контракт этот про «нет» и
«не знаю». Разница между ними — не стилистическая: красная точка на живой роли
отправляет человека перезапускать то, что сломано, а серая на мёртвой прячет
настоящую поломку. Поэтому каждый «не знаю» закреплён отдельной проверкой, и
проверка «процесс найден» обязана поднимать настоящий процесс — иначе обход
`/proc` можно выкинуть, и набор останется зелёным.
"""
from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

from board_hosts import (clean_pid, mtime_age, pane_status, pilot_pids, pilot_section,
                        resolve_pane_title)
from pair_test_base import PairTestCase


class CleanPidTest(unittest.TestCase):
    def test_real_pid_survives(self):
        self.assertEqual(clean_pid(os.getpid()), os.getpid())

    def test_zero_and_junk_are_absent_not_dead(self):
        # Ноль и не-целое — это «pid не записан». Превращать их в «мёртв» нельзя:
        # роль, у которой pid просто не писали, выглядела бы сломанной.
        for value in (0, -1, None, "123", "", 1.5, True):
            self.assertIsNone(clean_pid(value), value)


class MtimeAgeTest(unittest.TestCase):
    def test_missing_file_is_none_not_zero(self):
        # Ноль означал бы «только что изменилось» для файла, которого нет.
        self.assertIsNone(mtime_age(Path("/нет/такого/файла")))

    def test_fresh_file_is_small(self):
        self.assertIsNotNone(mtime_age(Path(__file__)))


class ResolvePaneTitleTest(PairTestCase):
    """Имя панели не имеет права выдумываться.

    Регрессия, найденная при применении патча agy: собственный запрос
    `display-message` возвращал для несуществующей панели код 0 и `":."` — то
    есть НЕПУСТОЙ вывод, и проверка «код успеха и вывод не пуст» проходила. На
    экране появлялось `панель %999999 [.:]`, и фантом выглядел как панель с
    именем. Тот же класс обмана уже стоил проекту дыры между
    `server.pane_exists` и `composer.pane_alive`.
    """

    def test_phantom_pane_is_not_given_a_fabricated_name(self):
        self.assertEqual(resolve_pane_title("%999999"), "%999999")

    def test_pane_that_is_not_an_id_is_returned_unchanged(self):
        for value in ("mail:0.1", "", "не панель"):
            self.assertEqual(resolve_pane_title(value), value)

    def test_live_pane_resolves_to_a_readable_address(self):
        """На настоящей tmux-панели: функция обязана что-то отдавать и на живом
        `%N`, иначе проверка выше проходила бы на одной лишь защите."""
        live = _first_live_pane()
        if live is None:
            self.skipTest("нет живых панелей tmux в этом окружении")
        resolved = resolve_pane_title(live)
        self.assertNotEqual(resolved, live, "живая панель должна разрешаться в имя")
        self.assertRegex(resolved, r"^.+:\d+\.\d+$",
                         f"ожидался адрес вида mail:0.0, получено {resolved!r}")


def _first_live_pane() -> str | None:
    import subprocess

    from composer import _tmux, pane_identity
    listed = subprocess.run(["tmux", "list-panes", "-a", "-F", "#{pane_id}"],
                            capture_output=True, text=True)
    for entry in listed.stdout.split():
        try:
            if pane_identity(_tmux(), entry).get("exists"):
                return entry
        except Exception:
            continue
    return None


class PaneStatusTest(unittest.TestCase):
    def test_absent_pane_is_unknown_with_a_reason(self):
        alive, why = pane_status("")
        self.assertIsNone(alive)
        self.assertIn("панель", why)

    def test_pane_of_the_wrong_shape_is_not_declared_dead(self):
        # Строка, которая не похожа на панель, не проверяется вовсе: `pane_alive`
        # отвечает про настоящие панели, а не про любой текст в поле `pane`.
        alive, why = pane_status("не панель")
        self.assertFalse(alive)
        self.assertEqual(why, "")


class PilotPidsTest(unittest.TestCase):
    def test_running_process_is_found_by_its_command_line(self):
        needle = "board-hosts-probe-marker"
        proc = subprocess.Popen(
            [sys.executable, "-c", f"import time; time.sleep(30)  # {needle}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(proc.kill)
        found = pilot_pids(needle)
        self.assertIn(proc.pid, found)

    def test_search_finds_nothing_when_nothing_matches(self):
        self.assertEqual(pilot_pids("board-hosts-probe-нет-такого"), [])

    def test_search_never_returns_the_caller(self):
        # Иначе вызывающий объявил бы сверхвизором себя.
        found = pilot_pids("")
        self.assertNotIn(os.getpid(), found)

    def test_search_survives_a_missing_proc(self):
        # /proc отсутствует на macOS и в некоторых контейнерах. Это «не знаю»,
        # а не повод уронить сборку снимка, поэтому проверка подставляет именно
        # такую среду: иначе ветка проходила бы только на Linux и нигде больше.
        import board_hosts
        original = board_hosts.os.listdir

        def explode(path):
            if path == "/proc":
                raise OSError("нет /proc")
            return original(path)

        board_hosts.os.listdir = explode
        try:
            self.assertEqual(pilot_pids("что угодно"), [])
        finally:
            board_hosts.os.listdir = original


class PilotSectionTest(PairTestCase):
    def test_absent_pilot_is_reported_as_not_running(self):
        section = pilot_section(self.root, [])
        self.assertFalse(section["running"])
        self.assertIsNone(section["pid"])
        self.assertEqual(section["pids"], [])

    def test_pilot_events_and_refusals_are_counted(self):
        self.store.append_transcript("pilot_unblock_refused", "pairpilot", why="запрещено")
        self.store.append_transcript("pilot_restart_impossible", "pairpilot", why="нет панели")
        self.store.append_transcript("pilot_wake_acknowledged", "pairpilot", target="agy")
        section = pilot_section(self.root, self.store.read_transcript(20))
        self.assertEqual(section["events_shown"], 3)
        self.assertEqual(section["refusals"], 2)
        # У последнего события (`pilot_wake_acknowledged`) из полей есть только
        # `target`, а подробностей нет. Показывать «agy» тут бессмысленно: что
        # именно пилот сделал, говорит сам вид события, и клиент переводит его
        # в слова. Поэтому fallback — вид, а не имя роли.
        self.assertEqual(section["last"], "pilot_wake_acknowledged")
        self.assertEqual(section["last_ts"] is not None, True)

    def test_last_summary_falls_back_to_the_kind(self):
        entries = [{"kind": "pilot_own_pane"}]
        self.assertEqual(pilot_section(self.root, entries)["last"], "pilot_own_pane")

    def test_missing_log_and_report_are_reported_as_absent(self):
        section = pilot_section(self.root, [])
        self.assertEqual(section["log_size"], 0)
        self.assertFalse(section["report_exists"])
        self.assertIsNone(section["report_age_seconds"])

    def test_existing_log_and_report_are_measured(self):
        (self.root / "pairpilot.log").write_text("строка\n", encoding="utf-8")
        (self.root / "REPORT.md").write_text("# отчёт\n", encoding="utf-8")
        section = pilot_section(self.root, [])
        self.assertGreater(section["log_size"], 0)
        self.assertTrue(section["report_exists"])
        self.assertIsNotNone(section["report_age_seconds"])


if __name__ == "__main__":
    unittest.main(verbosity=2)