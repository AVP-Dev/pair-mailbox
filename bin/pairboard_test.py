"""Тесты CLI дашборда (`bin/pairboard`).

Проверяется то, ради чего CLI и существует, а не то, что он делает внутри:

- **не находит состояние там, где его нет, и говорит об этом кодом, а не пустой
  страницей.** Тихая пустая страница — худший ответ на «покажи мне пару».
- **не трогает состояние.** Наблюдатель, который меняет то, за чем наблюдает, хуже
  наблюдателя; проверка сравнивает содержимое `.pair/` до и после.
- **решает `--state` от корня проекта.** От текущего каталога путь уезжает, и
  человек, запустивший из подкаталога, получает «пара не найдена».
- **`--watch` переживает битый проход.** Падение одного прохода не должно убивать
  наблюдение: файл на диске остаётся предыдущим, и страница об этом скажет.

Реальные запуски делаются как подпроцесс, потому что `signal` и `sys.exit`
нельзя проверить, вызвав `main()` в том же процессе: обработчик SIGINT не сработает
на самого себя, и проверка прошла бы, ничего не остановив.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PAIRBOARD = REPO_ROOT / "bin" / "pairboard"
SNAPSHOT_RE = ('<script id="snapshot" type="application/json">', "</script>")


class CliTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state = Path(self._tmp.name) / ".pair"
        self.state.mkdir(parents=True)
        (self.state / "state.json").write_text(json.dumps({
            "task": "дашборд", "mode": "fix", "autonomy": "auto", "writer": "agy",
            "phase": "implement", "round": 16, "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
            "roles": {"agy": {"pid": 1, "pane": "", "last_seen": "2026-01-01T00:00:00+00:00"}},
            "tasks": [{"id": 1, "text": "задача", "status": "open", "round": 16}],
        }, ensure_ascii=False), encoding="utf-8")
        self.out = Path(self._tmp.name) / "board.html"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_cli(self, *args: str, expect: int | None = 0) -> subprocess.CompletedProcess:
        proc = subprocess.run([sys.executable, str(PAIRBOARD), *args],
                              cwd=str(REPO_ROOT), capture_output=True, text=True,
                              timeout=60)
        if expect is not None:
            self.assertEqual(proc.returncode, expect,
                             f"код {proc.returncode}\nstdout: {proc.stdout}\nstderr: {proc.stderr}")
        return proc

    def snapshot_of(self, page: str) -> dict:
        start = page.index(SNAPSHOT_RE[0]) + len(SNAPSHOT_RE[0])
        return json.loads(page[start:page.index(SNAPSHOT_RE[1], start)])


class BuildOnceTest(CliTestCase):
    def test_default_out_is_the_repo_root(self):
        """Витрина по умолчанию — в корне репо, а не глубоко в .pair.

        Карточка board-to-root-and-live п.1: про витрину забывают, потому
        что она лежит глубоко и открывается руками. Дефолт в корень —
        однострочник, и этот тест его держит.
        """
        import importlib.machinery
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "pairboard_mod", str(PAIRBOARD),
            loader=importlib.machinery.SourceFileLoader(
                "pairboard_mod", str(PAIRBOARD)))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(module.DEFAULT_OUT, "board.html")
        self.assertFalse(module.DEFAULT_OUT.startswith(".pair/"))

    def test_writes_page_and_prints_openable_url(self):
        proc = self.run_cli("--state", str(self.state), "--out", str(self.out))
        self.assertTrue(self.out.exists())
        self.assertIn(f"file://{self.out}", proc.stdout)
        self.assertEqual(self.snapshot_of(self.out.read_text(encoding="utf-8"))
                         ["round"]["number"], 16)

    def test_one_shot_says_it_is_not_live(self):
        # Человек должен знать, что картинка не обновится сама: иначе он будет
        # смотреть на застывшую ленту и считать, что пара замолчала.
        proc = self.run_cli("--state", str(self.state), "--out", str(self.out))
        self.assertIn("--watch", proc.stdout)
        # Проверяется ТОЛЬКО `<head>`. Клиент упоминает `<meta http-equiv="refresh">`
        # в своём комментарии и в селекторе, поэтому поиск по всему файлу нашёл бы
        # объяснение, а не работающее автообновление.
        head = self.out.read_text(encoding="utf-8").split("</head>")[0]
        self.assertIsNone(re.search(r'<meta\s+http-equiv="refresh"', head),
                          "разовый запуск не должен включать автообновление")

    def test_relative_out_is_taken_from_project_root(self):
        # Запуск из чужого каталога не должен разбрасывать файл по машине.
        proc = subprocess.run([sys.executable, str(PAIRBOARD),
                               "--state", str(self.state), "--out", "board-тест.html"],
                              cwd=self._tmp.name, capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        written = REPO_ROOT / "board-тест.html"
        try:
            self.assertTrue(written.exists())
            self.assertIn(str(written), proc.stdout)
        finally:
            written.unlink(missing_ok=True)

    def test_missing_state_is_refused_with_a_reason(self):
        empty = Path(self._tmp.name) / "пусто"
        empty.mkdir()
        proc = self.run_cli("--state", str(empty), "--out", str(self.out), expect=2)
        self.assertIn("state.json", proc.stderr)
        self.assertFalse(self.out.exists(), "страница без состояния — это ложь")

    def test_state_is_never_modified(self):
        def contents() -> dict[str, bytes]:
            # Только файлы: `Store` создаёт служебные каталоги (mailbox, notes,
            # patches, obligations), и чтение каталога байтами падает с
            # IsADirectoryError — проверялось бы не то.
            return {path.name: path.read_bytes() for path in self.state.iterdir()
                    if path.is_file()}

        before = contents()
        self.run_cli("--state", str(self.state), "--out", str(self.out))
        after = contents()
        # `.lock` — служебный файл самого протокола: `Store` создаёт его при
        # обращении, и сервер делает ровно то же. Исключён не «на всякий случай»,
        # а потому что требование «ничего не пишется» относится к состоянию пары,
        # а `.lock` не состояние, а блокировка доступа к нему. Всё остальное
        # обязано совпасть побайтово.
        lock = {".lock"}
        self.assertEqual({k: v for k, v in after.items() if k not in lock},
                         {k: v for k, v in before.items() if k not in lock},
                         "дашборд изменил состояние, за которым наблюдает")
        self.assertEqual(set(after) - set(before), lock,
                         "дашборд создал в .pair/ что-то кроме служебного .lock")

    def test_print_json_writes_no_html(self):
        proc = self.run_cli("--state", str(self.state), "--print-json")
        data = json.loads(proc.stdout)
        self.assertEqual(data["round"]["writer"], "agy")
        self.assertFalse(self.out.exists())

    def test_limit_bounds_the_feed(self):
        transcript = self.state / "transcript.jsonl"
        with transcript.open("w", encoding="utf-8") as handle:
            for index in range(30):
                handle.write(json.dumps({"kind": "send", "role": "agy", "to": "a",
                                         "subject": f"письмо {index}"}) + "\n")
        proc = self.run_cli("--state", str(self.state), "--print-json", "--limit", "5")
        data = json.loads(proc.stdout)
        self.assertEqual(len(data["events"]), 5)
        self.assertEqual(data["counts"]["events_total"], 30)

    def test_stale_after_reaches_the_snapshot(self):
        proc = self.run_cli("--state", str(self.state), "--print-json",
                            "--stale-after", "42")
        self.assertEqual(json.loads(proc.stdout)["stale_after"], 42)


class WatchTest(CliTestCase):
    """`--watch` проверяется подпроцессом и только по фактам.

    Сигнал ловится только в главном потоке уже запущенного интерпретатора. Если
    отправить SIGINT, пока Python ещё грузится, процесс умрёт с кодом -2 и
    обработчик не успеет встать: это гонка ТЕСТА с запуском, а не поведение
    программы. Поэтому перед остановкой всегда ждётся первый файл — он
    появляется ровно после того, как обработчик установлен.
    """

    def start_watch(self, seconds: str = "1") -> subprocess.Popen:
        proc = subprocess.Popen(
            [sys.executable, str(PAIRBOARD), "--state", str(self.state),
             "--out", str(self.out), "--watch", seconds],
            cwd=str(REPO_ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self._stop, proc)
        return proc

    def wait_for_page(self, timeout: float = 40) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.out.exists():
                page = self.out.read_text(encoding="utf-8")
                if page.strip():
                    return page
            time.sleep(0.2)
        self.fail("--watch не создал файл за отведённое время")

    def test_watch_embeds_refresh_and_keeps_rewriting(self):
        self.start_watch()
        first = self.wait_for_page()
        head = first.split("</head>")[0]
        self.assertIn('<meta http-equiv="refresh" content="1">', head)
        self.assertTrue(self.snapshot_of(first)["watch"])

        # Событие в журнале обязано появиться в странице без повторного запуска:
        # в этом и смысл `--watch`.
        self.state.joinpath("transcript.jsonl").write_text(
            json.dumps({"kind": "send", "role": "agy", "to": "a",
                        "subject": "появилось в эфире"}) + "\n", encoding="utf-8")
        deadline = time.time() + 45
        seen = False
        while time.time() < deadline and not seen:
            seen = "появилось в эфире" in self.out.read_text(encoding="utf-8")
            if not seen:
                time.sleep(0.3)
        self.assertTrue(seen, "--watch не подхватил новое событие")

    def _stop(self, proc: subprocess.Popen) -> None:
        """Дожидается готовности, останавливает и проверяет код возврата.

        Код важен: `run_watch` ловит SIGINT специально, чтобы не оставить
        временный файл и не вернуть 130. Иначе гейт читал бы остановку как падение.
        """
        if proc.poll() is None and not self.out.exists():
            self.wait_for_page(timeout=40)
        if proc.poll() is None:
            proc.send_signal(2)  # SIGINT
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
                self.fail("--watch не остановился по SIGINT")
        self.assertEqual(proc.returncode, 0,
                         f"код остановки {proc.returncode}; "
                         f"остался временный файл: {list(self.out.parent.glob('*.tmp'))}")

    def test_interrupted_watch_leaves_no_temporary_file(self):
        proc = self.start_watch("3600")
        self.wait_for_page()
        self._stop(proc)
        self.assertTrue(self.out.exists())
        self.assertEqual(list(self.out.parent.glob("*.tmp")), [])

    def test_watch_survives_a_failing_pass(self):
        # Пока живой агент, состояние убирают посреди работы: наблюдение не
        # должно падать, потому что на диске осталась предыдущая, целая страница.
        proc = self.start_watch()
        self.wait_for_page()
        self.state.joinpath("state.json").unlink()
        self.state.mkdir(exist_ok=True)
        time.sleep(4)
        self.assertIsNone(proc.poll(), "--watch умер после неудачного прохода")
        head = self.out.read_text(encoding="utf-8").split("</head>")[0]
        self.assertIn('<meta http-equiv="refresh"', head)


class UsageTest(unittest.TestCase):
    def test_help_works(self):
        proc = subprocess.run([sys.executable, str(PAIRBOARD), "--help"],
                              cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("--watch", proc.stdout)
        self.assertIn("--print-json", proc.stdout)

    def test_script_is_executable_and_has_a_shebang(self):
        # bin/pairwatch — образец: гейт размеров считает файл исходником именно
        # потому, что у него есть исполняющий бит и шебанг.
        self.assertTrue(os.access(PAIRBOARD, os.X_OK), "bin/pairboard не исполняемый")
        self.assertTrue(PAIRBOARD.read_text(encoding="utf-8").startswith("#!"))


if __name__ == "__main__":
    unittest.main(verbosity=2)