#!/usr/bin/env python3
"""Тесты команд человека, которые только читают: `pairctl all/why`, обзор проектов.

Соответствуют `pairctl_read.py`. Сюда же едет проверка `bin/pairchat`, потому что
он печатает обзор из того же модуля: проверять их раздельно означало бы дважды
поднимать одну и ту же фикстуру ради одного и того же кода.

Запуск:  python3 mcp/pair_mailbox/test_pairctl_read.py
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
    """Видеть все проекты сразу, а не переключаться между окнами.

    Здесь же регрессия на `pairchat --once`: флага не было, а я всё равно советовал
    его набирать. Тест запускает скрипт как подпроцесс и требует, чтобы он сам
    завершился — иначе это снова ложь в документации.
    """

    def _project(self, name: str, *, phase: str = "scoping", writer=None, events: int = 0):
        root = Path(self._tmp.name) / name / ".pair"
        store = Store(root)
        store.mutate(lambda state: state.update({"phase": phase, "writer": writer,
                                                 "task": f"задача {name}"}))
        for _ in range(events):
            store.append_transcript("claim", writer or "opencode", writer=writer or "opencode")
        return root

    def setUp(self) -> None:
        super().setUp()
        # discover_projects по замыслу не теряет проект, в котором стоишь, — а мы стоим
        # в репозитории, где .pair есть. Без chdir каждый тест видел бы ещё и umputum.
        self._cwd = os.getcwd()
        os.chdir(self._tmp.name)

    def tearDown(self) -> None:
        os.chdir(self._cwd)
        os.environ.pop("PAIR_SCAN", None)
        super().tearDown()

    def test_discovers_only_directories_that_actually_have_state(self) -> None:
        good = self._project("good", events=1)
        # каталог без state.json — не проект, и в таблице у него быть не должно
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
        # где бы человек ни стоял, его собственный проект не должен пропасть из вида
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
        # пишем state.json без единого события: пара не двигалась, значит и молчание
        # не должно выглядеть как свежая активность
        root = self._project("idle", events=1)
        Store(root).mutate(lambda state: state.update({"task": "трогаем state"}))
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
            # последний столбец может содержать пробелы, поэтому сравниваем только
            # начала первых пяти: именно они должны совпадать на всех строках
            return [match.start() for match in re.finditer(r"\S+", line)][:count]

        reference = columns(lines[0])
        self.assertEqual(len(reference), 5)
        for line in lines[2:]:
            if line.strip():
                self.assertEqual(columns(line), reference,
                                 "столбцы разъехались:\n" + "\n".join(lines))

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
        # ровно наши два проекта, ничего лишнего из глобов по умолчанию
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
        # 3 — чтобы скрипт мог отличить «всё живо» от «кто-то встал»
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
        # ровно то, в чём я ошибался: флага не было, скрипт висел до Ctrl-C
        self.store.mutate(lambda state: state.update({"phase": "done", "writer": "writer-agent"}))
        self.store.append_transcript("claim", "writer-agent", writer="writer-agent")
        (self.root / "notes").mkdir(parents=True, exist_ok=True)
        (self.root / "notes" / "plan.md").write_text("план на две строки\n", encoding="utf-8")
        env = dict(os.environ, PAIR_STATE=str(self.root))
        try:
            done = subprocess.run([sys.executable, str(PAIRCHAT), "--once"],
                                  capture_output=True, text=True, timeout=30, env=env)
        except subprocess.TimeoutExpired:
            self.fail("--once не завершился сам: скрипт висит, как и до флага")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("ПИСАТЕЛЬ: writer-agent", done.stdout)
        self.assertIn("план на две строки", done.stdout)

    def test_naming_a_project_replaces_the_built_in_search(self) -> None:
        # иначе «посмотри ~/work/blog» показал бы пять проектов из глобов плюс blog
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
        self.assertIn("фаза", plain.splitlines()[0])
        for line in plain.splitlines():
            if "CLAIM" in line or "ADVANCE" in line:
                head = line.split("CLAIM")[0].split("ADVANCE")[0].strip()
                self.assertRegex(head, r"^(proj-one|proj-two)\s+\[\d\d:\d\d:\d\d\]$",
                                 "у блока нет метки проекта: " + line)

    def test_pairchat_shows_only_the_project_you_are_in(self) -> None:
        # Пользователь попросил именно этого: в папке проекта — диалог этого
        # проекта, без чужих. Смешанная выдача здесь вредна.
        mine = self._project("mine", events=1)
        self._project("theirs", events=1)
        os.environ["PAIR_SCAN"] = str(Path(self._tmp.name) / "*" / ".pair")
        cwd = os.getcwd()
        os.chdir(mine.parent)
        try:
            done = subprocess.run([sys.executable, str(PAIRCHAT), "--once"],
                                  capture_output=True, text=True, timeout=30)
        finally:
            del os.environ["PAIR_SCAN"]
            os.chdir(cwd)
        self.assertEqual(done.returncode, 0, done.stderr)
        plain = re.sub(r"\x1b\[[0-9;]*m", "", done.stdout)
        self.assertIn("mine", plain)
        self.assertNotIn("theirs", plain, "показан чужой проект без --all")

    def test_pairchat_outside_a_project_points_back_at_the_project(self) -> None:
        self._project("somewhere-else", events=1)
        cwd = os.getcwd()
        os.chdir(self._tmp.name)  # тут .pair нет
        try:
            done = subprocess.run([sys.executable, str(PAIRCHAT), "--once"],
                                  capture_output=True, text=True, timeout=30)
        finally:
            os.chdir(cwd)
        self.assertEqual(done.returncode, 1)
        text = done.stdout + done.stderr
        self.assertIn("--all", text, "не сказано, что делать, чтобы увидеть всё")
        self.assertIn("cd", text, "не сказано, что надо зайти в папку проекта")

    def test_agent_notes_are_shown_by_default_and_work_notes_are_not(self) -> None:
        # Заметки агентов (plan, finding...) — часть работы, их видно.
        # Мои рабочие заметки о самом pair-mailbox — нет, их сотни строк кода.
        for kind in ("plan", "finding", "verdict", "z-scratch"):
            path = self.root / "notes" / f"{kind}.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"содержимое заметки {kind}\n", encoding="utf-8")
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
        self.assertIn("содержимое заметки plan", done.stdout)
        self.assertIn("содержимое заметки verdict", done.stdout)
        self.assertNotIn("z-scratch", done.stdout, "рабочая заметка попала в выдачу")
        self.assertIn("содержимое заметки z-scratch", loud.stdout,
                      "--all-notes должен показывать и рабочие заметки")

    def test_long_lines_are_wrapped_to_a_readable_width(self) -> None:
        # Агенты пишут абзацами по 300+ символов; без переноса терминал рвёт
        # их посреди слова.
        self.store.mutate(lambda state: state.update({"phase": "plan"}))
        (self.root / "mailbox" / "peer").mkdir(parents=True, exist_ok=True)
        payload = " ".join(["слово%02d" % i for i in range(60)])
        (self.root / "mailbox" / "peer" / "000001-t.json").write_text(
            json.dumps({"seq": 1, "ts": "2026-10-02T20:00:00+00:00",
                        "from": "peer", "to": "writer-agent", "subject": "длинная тема " + payload,
                        "text": payload, "label": "Chat from peer: "}),
            encoding="utf-8")
        env = dict(os.environ, PAIR_STATE=str(self.root))
        done = subprocess.run([sys.executable, str(PAIRCHAT), "--once"],
                              capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual(done.returncode, 0, done.stderr)
        plain = re.sub(r"\x1b\[[0-9;]*m", "", done.stdout)
        for line in plain.splitlines():
            self.assertLessEqual(len(line), 105,
                                 "строка не перенесена: " + repr(line[:120]))

    def test_refuses_when_there_is_no_state(self) -> None:
        empty = Path(self._tmp.name) / "no-project"
        empty.mkdir()
        done = subprocess.run(
            [sys.executable, str(PAIRCHAT), "--once"],
            capture_output=True, text=True, timeout=30,
            env=dict(os.environ, PAIR_STATE=str(empty / ".pair")),
        )
        self.assertEqual(done.returncode, 1)
        self.assertIn("нет", done.stdout + done.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
