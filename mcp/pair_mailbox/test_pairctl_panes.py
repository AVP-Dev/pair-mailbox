#!/usr/bin/env python3
"""Тесты привязки ролей к панелям tmux: `pairctl pane` и `pairctl roster`.

Соответствуют `pairctl_panes.py`. Туда же уехала трёхсостояйная проверка
`pin_verdict`, и её тесты живут здесь по той же причине: состояние
`unknown` (проверить нечем) и состояние `bad` (проверено, что адрес мёртв)
различаются только в решениях двух вызывающих, а сами вызывающие — оба здесь.

Запуск:  python3 mcp/pair_mailbox/test_pairctl_panes.py
"""
from __future__ import annotations

import contextlib
import io
import os
import sys
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pair_test_base import PairTestCase  # noqa: E402
from server import main  # noqa: E402

class TestRosterPrune(PairTestCase):
    """`pairctl roster --prune`.

    Ростер — общая память пары, и в нём годами лежат записи умерших сессий: на
    живой панели agy числились ещё claude, codex и обе роли duo. Мёртвая запись
    занимает место, путает отчёт и портит skip. Удалять можно не молча и не по
    одному признаку, а по двум независимым фактам."""

    def ctl(self, *argv: str) -> int:
        return main(["--state", str(self.root), *argv])

    def dead_role(self, role: str, pane: str, pid: int = 999999) -> None:
        self.store.mutate(lambda st: st.setdefault("roles", {}).update(
            {role: {"pane": pane, "pid": pid, "last_seen": "then", "first_seen": "then"}}))

    def test_prune_removes_a_dead_role_whose_pane_is_gone(self) -> None:
        self.dead_role("ghost", "%999999")
        self.assertEqual(0, self.ctl("roster", "--prune"))
        self.assertNotIn("ghost", self.store.read()["roles"])

    def test_prune_keeps_a_dead_role_whose_pane_still_exists(self) -> None:
        """Одного мёртвого pid мало: после смерти агента могла остаться живая
        панель, и удалять запись рано.

        Подмена идёт по месту, где функция теперь живёт: `pane_exists`
        использует `pairctl_panes`, и подмена `server.pane_exists` больше не
        влияла бы ни на что — тест проверял бы настоящий tmux и проходил бы по
        неверной причине. Поэтому подменяется модуль потребителя, а не модуль,
        где функция определена.
        """
        self.dead_role("leftover", "%20")
        with mock.patch("pairctl_panes.pane_exists", return_value=True):
            self.assertEqual(0, self.ctl("roster", "--prune"))
        self.assertIn("leftover", self.store.read()["roles"])

    def test_prune_removes_a_dead_role_whose_pane_belongs_to_someone_living(self) -> None:
        """Третий случай, который и ждал в бою: панель жива, но принадлежит другой
        живой роли. Запись не просто устарела — она указывает не туда."""
        self.dead_role("claude", "%20")
        self.store.mutate(lambda st: st.setdefault("roles", {}).update(
            {"agy": {"pane": "%20", "pid": os.getpid()}}))
        with mock.patch("pairctl_panes.pane_exists", return_value=True):
            self.assertEqual(0, self.ctl("roster", "--prune"))
        roles = self.store.read()["roles"]
        self.assertNotIn("claude", roles)
        self.assertIn("agy", roles, "живую роль трогать нельзя")

    def test_prune_never_touches_a_living_role(self) -> None:
        self.store.mutate(lambda st: st.setdefault("roles", {}).update(
            {"agy": {"pane": "%20", "pid": os.getpid()}}))
        self.assertEqual(0, self.ctl("roster", "--prune"))
        self.assertIn("agy", self.store.read()["roles"])

    def test_prune_refuses_to_remove_the_writer(self) -> None:
        """Иначе можно было бы очистить ростер и вместе с ним держателя права,
        не сказав ни слова."""
        self.writer.tool_pair_claim(reason="writer")
        self.dead_role("writer-agent", "%999999")
        self.assertEqual(2, self.ctl("roster", "--prune"))
        self.assertIn("writer-agent", self.store.read()["roles"])

    def test_roster_without_prune_only_looks(self) -> None:
        self.dead_role("ghost", "%999999")
        self.assertEqual(0, self.ctl("roster"))
        self.assertIn("ghost", self.store.read()["roles"])

    def test_pruning_is_in_the_audit_trail(self) -> None:
        self.dead_role("ghost", "%999999")
        self.ctl("roster", "--prune")
        self.assertIn("roster_pruned", [e["kind"] for e in self.store.read_transcript()])



class TestPaneOverride(PairTestCase):
    """Переопределение привязки панели рукой человека (`pairctl pane`).

    Причина появления — измерена, а не предположена: `_register` берёт адрес из
    `TMUX_PANE` окружения процесса и перезаписывает `pane` на каждом вызове
    любого инструмента. Поэтому ручная правка `state.json` не чинила ничего —
    «починка» жила ровно до следующего звонка. Ключ `pane_pin` читается раньше
    окружения и потому переживает перерегистрацию.

    Цена такого ключа — риск стать дырой в изоляции между проектами, поэтому
    закрепляемый адрес обязан проходить ту же проверку, что и снятый
    автоматически: панель жива И панель наша.
    """

    def setUp(self) -> None:
        super().setUp()
        import server
        self.server = server

    def ctl(self, *argv: str) -> int:
        return main(["--state", str(self.root), *argv])

    def pinned(self) -> dict:
        return (self.store.read().get("roles") or {}).get("opencode", {})

    def with_pane(self, pane: str | None) -> None:
        """Окружение процесса: либо живая панель набора, либо её отсутствие.

        Восстанавливается та панель, с которой набор стартовал, а не то, что лежало
        в переменной в момент вызова: базовый `setUp` переменную уже вычистил, и
        cleanup, возвращающий «прежнее», стирал бы то, что `tearDown` уже
        восстановил. Порядок unittest — `tearDown`, потом cleanup, — и из-за этого
        все следующие тесты набора начинали молча пропускаться, а набор оставался
        зелёным. Проверено: пять тестов превратились в skip, и ни один не упал.
        """
        before = self._pane_at_startup
        if pane is None:
            os.environ.pop("TMUX_PANE", None)
        else:
            os.environ["TMUX_PANE"] = pane

        def restore() -> None:
            if before is None:
                os.environ.pop("TMUX_PANE", None)
            else:
                os.environ["TMUX_PANE"] = before

        self.addCleanup(restore)

    def live_pane(self) -> str:
        pane = self._pane_at_startup
        if not pane:
            self.skipTest("набор запущен вне tmux")
        return pane

    def as_ours(self):
        """Считать панель набора своей, без подмены результата проверки.

        Набор работает в состоянии во временном каталоге, а панель — в реальном
        проекте, поэтому подлинность панели здесь всегда «чужая». Подменять её на
        «своя» надо явно: иначе тест проверял бы только отказ, а не принятие.

        Подменяются оба вопроса. `_register` спрашивает «панель этого проекта?»,
        а `pair_deliver` — «не чужая ли?». С появлением второй проверки подмена
        осталась на первой, и проверка принятия стала проверкой отказа: тест
        требовал записи панели, а получал её снятие.
        """
        return mock.patch.multiple(
            "composer",
            pane_is_foreign=mock.Mock(return_value=(False, "")),
            pane_belongs_to_project=mock.Mock(return_value=(True, "")),
        )

    def bind(self, role: str, pane: str, pid: int) -> None:
        self.store.mutate(lambda st: st.setdefault("roles", {}).update(
            {role: {"pane": pane, "pid": pid, "first_seen": "then",
                    "last_seen": "then", "reads": 0, "sends": 0}}))

    def register(self, role: str) -> None:
        """Прогнать регистрацию роли — то, что делает сервер при каждом вызове.

        Через `mutate`, а не на чтении: `_register` правит состояние на месте, и
        набор, скормивший ему копию, проверял бы копию, а не то, что сервер
        сохранил бы в следующий раз.
        """
        session = self.server.PairSession(role, self.store)
        self.store.mutate(session._register)

    def test_the_override_survives_a_reregistration(self) -> None:
        """Главное свойство, ради которого всё затевалось.

        Без него переопределение — запись в state.json руками, которая протухает
        через один вызов, то есть ровно та поломка, которую человек и чинит.
        """
        pane = self.live_pane()
        self.with_pane(pane)
        self.bind("opencode", pane, 999999)
        with self.as_ours():
            self.assertEqual(0, self.ctl("pane", "--role", "opencode", pane))

        # Записанная панель уезжает, а панели в окружении нет. Так выглядит
        # перерегистрация сервера, который запустили не из той панели.
        #
        # Увод в сторону сделан явно: `pairctl pane` сам пишет `pane` при
        # установке, поэтому без этого тест проходил бы и при полностью
        # отключённом переопределении — проверить было нечего.
        self.store.mutate(lambda st: st["roles"]["opencode"].update(
            {"pane": "%12345"}))
        self.with_pane(None)
        with self.as_ours():
            self.register("opencode")
        self.assertEqual(pane, self.pinned().get("pane"),
                         "переопределение должно переживать потерю TMUX_PANE")
        self.assertEqual(pane, self.pinned().get("pane_pin"),
                         "закрепление обязано остаться, а не сработать один раз")

    def test_a_stale_override_is_dropped_and_says_why(self) -> None:
        """Закрепление протухшей панели не может висеть вечно.

        Иначе доставка уходила бы в никуда молча, и человек считал бы, что роль
        привязана. Снятие обязано быть замечено в транскрипте: молча снятое
        переопределение неотличимо от того, которого не было никогда.
        """
        self.with_pane(self.live_pane())
        self.bind("opencode", self._pane_at_startup, 999999)
        # Адрес несуществующей панели отвергается на входе, а не «на всякий случай».
        #
        # Проверяется ИМЕННО «панели нет», а не просто «отказано»: панель, которой
        # нет, отвергается ещё и проверкой на чужую, потому что tmux не сообщает
        # для неё cwd. Утверждение «отказано» проходило бы и при выкинутой
        # проверке живости — проверено мутацией.
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(1, self.ctl("pane", "--role", "opencode", "%987654"))
        self.assertIn("больше нет", stderr.getvalue())
        self.assertNotIn("pane_pin", self.pinned())

        # Теперь подложим протухшее закрепление напрямую — так оно выглядит после
        # того, как человек закрыл окно, а состояние осталось.
        self.store.mutate(lambda st: st["roles"]["opencode"].update(
            {"pane_pin": "%987654", "pane_pin_why": "закреплено человеком"}))
        self.register("opencode")
        self.assertNotIn("pane_pin", self.pinned(),
                         "адрес мёртвой панели обязан быть снят")
        kinds = [row["kind"] for row in self.store.read_transcript()]
        self.assertIn("pane_pin_dropped", kinds)

    def test_a_foreign_pane_is_refused(self) -> None:
        """Переопределение не должно стать дырой в изоляции между проектами.

        Именно для этого адрес проверяется. Если бы проверки не было, человек мог
        бы одним вводом направить письма роли в чужой проект — и сломал бы то,
        что чинили пункты 1 и 2.
        """
        self.with_pane(self.live_pane())
        self.bind("opencode", self._pane_at_startup, 999999)
        with mock.patch("composer.pane_is_foreign",
                        return_value=(True, "другой прокт: /somewhere/else")):
            self.assertEqual(1, self.ctl("pane", "--role", "opencode", "%1"))
        self.assertNotIn("pane_pin", self.pinned())

    def test_without_an_override_the_pane_still_comes_from_tmux(self) -> None:
        """Переопределение обязано быть узким: без него поведение прежнее.

        Иначе это не «переопределение», а новое правило, и агент, который и так
        работает, вдруг окажется не на той панели.
        """
        pane = self.live_pane()
        self.with_pane(pane)
        self.bind("opencode", "старое", 999999)
        with self.as_ours():
            self.register("opencode")
        from composer import _tmux, session_address
        expected = session_address(_tmux(), pane) or pane
        self.assertEqual(expected, self.pinned().get("pane"))

    def test_clearing_returns_the_pane_to_tmux(self) -> None:
        """Снятие переопределения обязано вернуть управление окружению.

        Иначе единственный способ вернуть роль на её панель — править state.json,
        что и было исходной поломкой.
        """
        pane = self.live_pane()
        self.with_pane(pane)
        self.bind("opencode", pane, 999999)
        with self.as_ours():
            self.assertEqual(0, self.ctl("pane", "--role", "opencode", pane))
        self.assertEqual(0, self.ctl("pane", "--role", "opencode", "--clear"))
        self.assertNotIn("pane_pin", self.pinned())
        with self.as_ours():
            self.register("opencode")
        from composer import _tmux, session_address
        self.assertEqual(session_address(_tmux(), pane) or pane,
                         self.pinned().get("pane"))

    def test_the_override_is_not_logged_on_every_call(self) -> None:
        """Журнал не должен наполняться одинаковыми строками.

        Регистрация идёт на каждом вызове любого инструмента. Запись на каждом
        вызове съедала бы ровно тот потолок транскрипта, который этот раунд и
        чинит, то есть переопределение стало бы новым источником шума.
        """
        pane = self.live_pane()
        self.with_pane(None)
        self.bind("opencode", pane, 999999)
        with self.as_ours():
            self.assertEqual(0, self.ctl("pane", "--role", "opencode", pane))
        for _ in range(3):
            self.register("opencode")
        kinds = [row["kind"] for row in self.store.read_transcript()]
        self.assertLessEqual(
            kinds.count("pane_pin_honoured"), 1,
            "запись должна появляться на изменении, а не на каждом вызове")


    def test_positional_role_and_address_bind_without_flag(self) -> None:
        """Позиционная форма из карточки: `pane <role> <addr>` без --role.

        Иначе при перезапуске окон чинить привязку можно только длинной
        формой, а карточка требует именно `pane <role> <tmux-pane>`.
        Окружение сброшено специально: иначе $TMUX_PANE-fallback
        замаскировал бы сломанный разбор позиционных (проверено мутацией).
        """
        pane = self.live_pane()
        self.with_pane(None)
        self.bind("opencode", pane, 999999)
        with self.as_ours():
            self.assertEqual(0, self.ctl("pane", "opencode", pane))
        self.assertEqual(pane, self.pinned().get("pane_pin"))

    def test_tmux_pane_env_is_used_only_with_current_flag(self) -> None:
        """$TMUX_PANE берётся только с явным --current (регрессия %2205).

        Без флага `pane <role>` из живой панели — только показ: иначе два
        опроса подряд (`pane opencode` + `pane pi` из одного окна) молча
        сажают обе роли на одну панель, и супервизор пропускает их как
        неразличимые. Измерено на живом состоянии 2026-10-06.
        """
        pane = self.live_pane()
        self.with_pane(pane)
        self.bind("opencode", pane, 999999)
        with self.as_ours():
            self.assertEqual(0, self.ctl("pane", "opencode"))
        self.assertNotIn("pane_pin", self.pinned(),
                         "показ из живой панели обязан ничего не писать")
        with self.as_ours():
            self.assertEqual(0, self.ctl("pane", "--role", "opencode", pane))
        with self.as_ours():
            self.assertEqual(0, self.ctl("pane", "opencode"))
        self.assertEqual(pane, self.pinned().get("pane_pin"))
        self.store.mutate(lambda st: (st["roles"]["opencode"].pop("pane_pin", None),
                                      st["roles"]["opencode"].pop("pane_pin_why", None)))
        with self.as_ours():
            self.assertEqual(0, self.ctl("pane", "opencode", "--current"))
        self.assertEqual(pane, self.pinned().get("pane_pin"))

    def test_show_when_no_address_and_no_tmux_pane(self) -> None:
        """Без address и без $TMUX_PANE — показ, а не выдуманный адрес."""
        pane = self.live_pane()
        self.with_pane(None)
        self.bind("opencode", pane, 999999)
        with self.as_ours():
            self.assertEqual(0, self.ctl("pane", "--role", "opencode", pane))
        self.with_pane(None)
        self.assertEqual(0, self.ctl("pane", "--role", "opencode"))
        self.assertEqual(pane, self.pinned().get("pane_pin"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
