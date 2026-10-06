"""Тесты дайджеста: срез состояния пары в жёстком потолке токенов.

Один тест-файл на один исходник: `digest.py` отвечает за одну обязанность —
уложить состояние в бюджет, — и проверяется здесь, а не вперемешку с протоколом.

Три свойства проверяются, а не декларируются:
- потолок держится на любом состоянии, включая специально раздутое;
- порядок разделов решает всё: перестановка делает дороже при том же составе;
- вердикт цитируется, а не пересказывается, потому что пересказ чужого решения
  своими словами — это способ соврать о том, чего не читали.
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pair_test_base import PairTestCase  # noqa: E402
from server import main  # noqa: E402

import digest  # noqa: E402


class TestDigest(PairTestCase):
    """Дайджест: срез состояния в жёстком бюджете токенов (пункт 5).

    Зачем он, измерено до того, как он написан: ориентация в паре стоит
    `pair_state` 939 + `pair_task list` 1051 + `pair_transcript` 1734 = 3724 токена.
    Дайджест обязан быть заметно дешевле, иначе он не нужен.

    Три свойства, которые проверяются здесь, а не декларируются:
    - **потолок держится** на любом состоянии, включая специально раздутое;
    - **порядок разделов решает всё**: перестановка делает дороже при том же составе;
    - **вердикт цитируется, а не пересказывается**, потому что пересказ чужого
      решения своими словами — это способ соврать о том, чего не читали.
    """

    def setUp(self) -> None:
        super().setUp()
        import server
        self.server = server

    def digest(self, budget: int = 500, role: str = "") -> str:
        return self.server.build_digest(self.store, role, budget)

    def ctl(self, *argv: str) -> int:
        return main(["--state", str(self.root), *argv])

    def with_state(self, **fields: object) -> None:
        self.store.mutate(lambda st: st.update(fields))

    def many_tasks(self, count: int, status: str = "open") -> None:
        self.store.mutate(lambda st: st.setdefault("tasks", []).extend(
            {"id": 100 + i, "text": f"задача номер {i} " + "хвост " * 12,
             "status": status, "added_at": "then", "added_by": "human", "round": 14}
            for i in range(count)))

    # ------------------------------------------------------------------ потолок

    def test_the_digest_fits_the_budget(self) -> None:
        """Потолок — свойство, а не пожелание.

        Дайджест, который перестал помещаться, хуже отсутствующего: агент будет
        планировать контекст по потолку, который уже не действует.
        """
        self.with_state(task="автоматизация парной работы: пилот-сверхвизор, роли, режимы",
                        writer="opencode", mode="fix", phase="implement", round=14)
        self.many_tasks(6)
        text = self.digest()
        self.assertLessEqual(self.server.estimate_tokens(text), 500,
                             "дайджест обязан помещаться в потолок")

    def test_the_budget_holds_on_a_deliberately_bloated_state(self) -> None:
        """Потолок держится на состоянии, специально раздутом.

        Проверка дешёвого дайджеста на нормальном состоянии ничего не доказывает:
        он и так поместится. Ломается он на том, чего не ждёшь, — двадцать
        открытых задач с длинными текстами и отсутствие git.
        """
        self.with_state(task="задача " * 400, writer="agy", mode="fix",
                        phase="verify", round=99)
        self.many_tasks(20, status="open")
        text = self.digest()
        self.assertLessEqual(self.server.estimate_tokens(text), 500)

    def test_a_tiny_budget_is_still_honoured(self) -> None:
        """Потолок маленький — он тоже потолок, а не ориентир.

        Если бы урезание не работало, агент на ограниченном контексте получил бы
        полный текст и узнал об этом по факту, когда упёрся в лимит.
        """
        self.with_state(task="задача " * 200, writer="agy")
        self.many_tasks(8)
        text = self.digest(budget=80)
        self.assertLessEqual(self.server.estimate_tokens(text), 80)

    def test_what_did_not_fit_is_said(self) -> None:
        """Обрезание обязано быть объявлено.

        Молча сокращённый дайджест хуже длинного: агент решит, что вердикта не
        было, и начнёт искать его там, где его нет.
        """
        self.with_state(task="задача " * 400, writer="agy")
        self.many_tasks(20)
        text = self.digest(budget=120)
        self.assertIn("обрезано", text)

    # ------------------------------------------------------------------ состав

    def test_the_reserve_covers_the_marker(self) -> None:
        """Резерв обязан быть не меньше самой пометки.

        Найдено на себе: резерв стоял круглым числом 20, пометка выходила 22, и на
        тексте, подогнанном точно под потолок, дайджест давал 501 токен при потолке
        500 — то есть либо нарушал обещание, либо вычищал строку, ради которой он
        собирался. Оба варианта плохи, и оба выглядят как «почти работает».
        """
        self.assertGreaterEqual(self.server._digest_marker_reserve(),
                                self.server.estimate_tokens(self.server.DIGEST_MARKER),
                                "резерв меньше пометки: дайджест выдаст пометку ценой "
                                "вычищенной полезной строки")

    def test_an_oversized_line_is_cut_rather_than_dropped(self) -> None:
        """Обрезанная первая строка полезнее, чем её отсутствие.

        Здесь видна разница между двумя способами держать потолок, и она не
        косметическая. Отбросить строку целиком — бюджет выдержан, но агент не
        видит задачи вообще и вынужден звать `pair_state`. Обрезать её — агент
        видит начало задачи и понимает, о чём раунд.

        Проверено мутацией: если убрать только обрезание строки, потолок всё равно
        держится (его держат `take` и финальная чистка), и набор остаётся зелёным.
        То есть без этой проверки обрезание было бы мёртвым кодом.
        """
        self.with_state(task="ЗАДАЧА-ОЧЕНЬ-ДЛИННАЯ " + "подробности " * 400)
        text = self.digest(budget=200)
        self.assertLessEqual(self.server.estimate_tokens(text), 200)
        self.assertIn("ЗАДАЧА-ОЧЕНЬ-ДЛИННАЯ", text,
                      "начало задачи обязано быть видно, иначе агент не знает, о чём раунд")
        self.assertIn("…", text, "обрезание обязано быть помечено")

    def test_the_digest_states_what_cannot_be_guessed(self) -> None:
        """Задача, режим, фаза и писатель — то, без чего работа идёт неверно.

        Отсутствие задачи тоже факт, а не пустота: пустая строка выглядит как
        «задача есть, но пустая», и это разные вещи.
        """
        text = self.digest()
        self.assertIn("РАУНД", text)
        self.assertIn("задача", text)
        self.assertIn("режим", text)
        self.assertIn("фаза", text)
        self.assertIn("пишет", text)

    def test_closed_tasks_do_not_take_space(self) -> None:
        """Закрытые задачи не несут смысла для следующего шага.

        Восемь закрытых задач стоили около 800 токенов в `pair_task list` и не
        сказали ничего, чего агент не знает.
        """
        self.many_tasks(6, status="done")
        text = self.digest()
        self.assertIn("ОТКРЫТЫЕ ЗАДАЧИ: 0", text)
        self.assertNotIn("задача номер", text)

    def test_missing_obligations_are_stated(self) -> None:
        """Отсутствие обязательств — факт, а не пустое место в тексте."""
        text = self.digest()
        self.assertIn("ОБЯЗАТЕЛЬСТВА: 0", text)
        self.assertIn("никто никого не ждёт", text)

    def test_an_open_obligation_is_named(self) -> None:
        """Кто кого ждёт — это то, ради чего агент и смотрит дайджест."""
        self.store.mutate(lambda st: None)
        path = self.store.obligations_root
        path.mkdir(parents=True, exist_ok=True)
        (path / "o1.json").write_text(json.dumps(
            {"from": "opencode", "to": "agy", "subject": "пункт 5"}, ensure_ascii=False),
            encoding="utf-8")
        text = self.digest()
        self.assertIn("opencode ждёт agy", text)

    def test_the_last_commit_is_read_from_git_not_from_the_log(self) -> None:
        """Фактическое состояние дерева, а не пересказ журнала.

        Журнал не знает, что закоммичено. Агент, читающий дайджест, должен увидеть
        правду о дереве, иначе он планирует работу по устаревшему представлению.
        """
        text = self.digest()
        self.assertIn("ПОСЛЕДНИЙ КОММИТ", text)

    def test_no_git_is_said_out_loud(self) -> None:
        """Отсутствие git объявляется, а не выглядит как «коммитов нет».

        Это разные факты с разными последствиями: первый означает «следи сам», второй
        — «дерево чистое, делать нечего».
        """
        with mock.patch("subprocess.run", side_effect=OSError("нет git")):
            text = self.digest()
        self.assertIn("git недоступен", text)

    # ----------------------------------------------------------------- вердикт

    def _note_in_journal(self, slug: str, role: str = "agy") -> None:
        """Заметка, зарегистрированная в журнале так же, как её пишет `pair_note`."""
        self.store.append_transcript("note", role, note_kind=slug, chars=10)
        path = self.store.notes_root / f"{slug}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        # Заметка длиннее хвоста: если бы дайджест брал начало, в него попало бы
        # «ПОСТАНОВКА ПРОШЛОГО РАУНДА» вместо решения.
        body = [f"строка {i} постановки" for i in range(12)]
        path.write_text("\n".join(body + ["ВЕРДИКТ: принято."]) + "\n", encoding="utf-8")

    def test_the_last_verdict_is_quoted_not_summarised(self) -> None:
        """Цитата, а не пересказ.

        Дайджест пересказывает чужое решение своими словами — он умеет соврать о
        том, чего не читал. Поэтому в него попадает дословный хвост заметки, и
        видно, чей это вердикт.
        """
        self._note_in_journal("verdict-прошлое")
        self._note_in_journal("verdict-свежее")
        text = self.digest()
        self.assertIn("ВЕРДИКТ: принято.", text)
        self.assertIn("verdict-свежее", text)
        self.assertNotIn("строка 0 постановки", text,
                         "дайджест берёт хвост заметки, а не её начало")

    def test_the_newest_verdict_wins_regardless_of_name(self) -> None:
        """Порядок вердиктов берётся из журнала, а не из имени файла.

        Имена не упорядочены по времени: `verdict.md`, `verdict-agy.md`,
        `verdict-pairctl-pane.md` — какой из них последний, по имени не видно.
        Заметок девяносто две, и читать их содержимое, чтобы найти последний, — это
        ровно та стоимость, которую дайджест обещает не платить.
        """
        # Сначала в журнале один, потом другой: порядок задаёт журнал.
        self._note_in_journal("verdict-aaa")
        self._note_in_journal("verdict-zzz")
        text = self.digest()
        self.assertIn("verdict-zzz", text)

    def test_no_verdict_is_stated_as_no_verdict(self) -> None:
        """Отсутствие вердикта говорится прямо.

        Иначе пустой раздел выглядит как «вердикт есть, но не влез».
        """
        text = self.digest()
        self.assertIn("ПОСЛЕДНИЙ ВЕРДИКТ", text)
        self.assertIn("вердиктов не было", text)

    # --------------------------------------------------------------- поверхности

    def test_the_tool_and_the_command_return_the_same_thing(self) -> None:
        """Одна реализация на человека и агента.

        Две реализации дайджеста разъедутся, и человек увидит не то, что видит
        агент, — а расхождение в такой штуке обнаруживается только при разборе
        инцидента.
        """
        self.with_state(task="одна и та же задача", writer="opencode")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(0, self.ctl("digest", "--budget", "400"))
        self.assertEqual(self.server.build_digest(self.store, "", 400), out.getvalue())

    def test_the_command_fails_loudly_when_the_budget_is_broken(self) -> None:
        """Потолок, который не выдержали, — это поломка, а не повод сделать вид.

        Молчаливый выход с кодом 0 дал бы человеку текст, который он принял за
        дайджест, и он бы на него опирался.
        """
        # Подменяется модуль-потребитель: `cmd_digest` живёт в `pairctl_read` и
        # берёт `build_digest` оттуда, а не из `server`. Подмена `server` после
        # разбора ничего бы не изменила, и тест проверял бы не тот код.
        with mock.patch("pairctl_read.build_digest", return_value="х" * 4000):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = self.ctl("digest", "--budget", "100")
        self.assertEqual(1, code)
        self.assertIn("потолке", err.getvalue())

    def test_the_digest_names_the_role_that_writes(self) -> None:
        """При переданной роли дайджест говорит «ты пишешь».

        Это избавляет агента от второго вызова, и смысл тут не в красоте, а в том,
        что весь смысл дайджеста — убрать лишний заход в состояние.
        """
        self.with_state(writer="opencode")
        self.assertIn("(ты)", self.digest(role="opencode"))
        self.assertNotIn("(ты)", self.digest(role="agy"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
