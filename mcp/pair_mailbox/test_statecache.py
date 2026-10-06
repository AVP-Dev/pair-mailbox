"""Тесты отпечатка состояния для кэша `pair_state`.

Один тест-файл на один исходник: `statecache.py` отвечает за одно — отпечаток
того, что читает полный ответ, — и проверяется здесь, а не в `test_protocol.py`
вместе с сервером.

Проверяется в первую очередь противоположное ложному «unchanged»: отпечаток
обязан меняться, когда ответ изменился. Промах в эту сторону возвращает агенту
заглушку с правдой, которой нет, и это хуже, чем лишний полный ответ.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pair_test_base import PairTestCase  # noqa: E402

import statecache  # noqa: E402


class TestCacheDisabled(unittest.TestCase):
    def test_enabled_when_env_is_absent(self) -> None:
        self.assertFalse(statecache.cache_disabled({}))

    def test_disabled_by_the_documented_variable(self) -> None:
        """Выключатель, обещанный комментарием, обязан действительно выключать.

        Тесты, которым нужен живой ответ на каждый вызов, полагаются на него: без
        него проверка закрывающего факта читала бы заглушку и проходила, ничего не
        проверяя.
        """
        self.assertTrue(
            statecache.cache_disabled({statecache.CACHE_DISABLED_ENV: "1"})
        )

    def test_reads_the_real_environment_by_default(self) -> None:
        """Без аргумента берётся окружение процесса, а не пустой словарь."""
        original = os.environ.get(statecache.CACHE_DISABLED_ENV)
        os.environ[statecache.CACHE_DISABLED_ENV] = "1"
        try:
            self.assertTrue(statecache.cache_disabled())
        finally:
            if original is None:
                os.environ.pop(statecache.CACHE_DISABLED_ENV, None)
            else:
                os.environ[statecache.CACHE_DISABLED_ENV] = original


class TestFingerprintSeesEverythingTheAnswerReads(unittest.TestCase):
    def base_state(self) -> dict:
        return {
            "task": "t", "mode": "fix", "phase": "implement", "round": 1,
            "writer": "opencode", "evidence": [], "human_ok": False,
            "roles": {"opencode": {"pid": 1, "last_seen": "x"}},
        }

    def test_same_state_gives_same_fingerprint(self) -> None:
        state = self.base_state()
        self.assertEqual(
            statecache.fingerprint(state, obligation_names=[], inbox_names=[]),
            statecache.fingerprint(state, obligation_names=[], inbox_names=[]),
        )

    def test_updated_at_alone_does_not_change_it(self) -> None:
        """`updated_at` меняется на каждом зове само по себе.

        Включённый в отпечаток, он делал бы кэш бесполезным: заглушка не
        возвращалась бы никогда.
        """
        first = dict(self.base_state(), updated_at="2026-01-01T00:00:00+00:00")
        second = dict(first, updated_at="2026-01-01T00:00:01+00:00")
        self.assertEqual(statecache.fingerprint(first), statecache.fingerprint(second))

    def test_last_seen_alone_does_not_change_it(self) -> None:
        """`last_seen` — тоже след регистрирования, а не содержание ответа."""
        first = {"roles": {"pi": {"pid": 5, "last_seen": "a"}}}
        second = {"roles": {"pi": {"pid": 5, "last_seen": "b"}}}
        self.assertEqual(statecache.fingerprint(first), statecache.fingerprint(second))

    def test_new_evidence_changes_it(self) -> None:
        """Ровно тот случай, который чинился: evidence пишется между зовами.

        Раньше ключ его не видел, и агент получал заглушку без
        `evidence_this_round` — то есть без ровно того, ради чего звал `pair_state`.
        """
        before = dict(self.base_state(), evidence=[])
        after = dict(self.base_state(), evidence=[{"kind": "test", "value": "1 passed"}])
        self.assertNotEqual(statecache.fingerprint(before), statecache.fingerprint(after))

    def test_human_ok_changes_it(self) -> None:
        before = dict(self.base_state(), human_ok=False)
        after = dict(self.base_state(), human_ok=True)
        self.assertNotEqual(statecache.fingerprint(before), statecache.fingerprint(after))

    def test_verdicts_change_it(self) -> None:
        before = dict(self.base_state(), verdicts=[])
        after = dict(self.base_state(), verdicts=[{"role": "pi"}])
        self.assertNotEqual(statecache.fingerprint(before), statecache.fingerprint(after))

    def test_patches_change_it(self) -> None:
        before = dict(self.base_state(), patches={})
        after = dict(self.base_state(), patches={"p1": {"applied": True}})
        self.assertNotEqual(statecache.fingerprint(before), statecache.fingerprint(after))

    def test_a_new_pid_changes_it(self) -> None:
        """Перезапуск соседа меняет ответ, и агент обязан это увидеть."""
        before = {"roles": {"pi": {"pid": 1, "last_seen": "a"}}}
        after = {"roles": {"pi": {"pid": 2, "last_seen": "a"}}}
        self.assertNotEqual(statecache.fingerprint(before), statecache.fingerprint(after))

    def test_a_new_obligation_file_changes_it(self) -> None:
        """Обязательства лежат на диске, в state.json их нет.

        Если отпечаток видит только state, открытое обязательство не меняет
        ничего — и агент читает «никто никого не ждёт» о том, что его ждут.
        """
        state = self.base_state()
        self.assertNotEqual(
            statecache.fingerprint(state, obligation_names=["pi.json"]),
            statecache.fingerprint(state, obligation_names=[]),
        )

    def test_a_new_inbox_file_changes_it(self) -> None:
        state = self.base_state()
        self.assertNotEqual(
            statecache.fingerprint(state, inbox_names=["1-pi.json"]),
            statecache.fingerprint(state, inbox_names=[]),
        )

    def test_obligation_order_does_not_change_it(self) -> None:
        """Порядок имён не содержание: два вызова не должны различаться из-за него."""
        state = self.base_state()
        self.assertEqual(
            statecache.fingerprint(state, obligation_names=["a.json", "b.json"]),
            statecache.fingerprint(state, obligation_names=["b.json", "a.json"]),
        )


class TestGitSnapshotOnlyMattersWithVerdicts(unittest.TestCase):
    def test_snapshot_ignored_without_verdicts(self) -> None:
        """Без вердиктов снимок git не читается и в отпечаток не входит.

        Иначе агент, не публиковавший ни одного вердикта, платил бы git-вызов
        на каждом `pair_state` впустую.
        """
        state = {"evidence": []}
        calls = []

        def snapshot():
            calls.append(1)
            return "head-abc"

        statecache.fingerprint(state, git_snapshot=snapshot)
        self.assertEqual(calls, [])

    def test_snapshot_included_when_verdicts_exist(self) -> None:
        """Вердикт есть — его stale зависит от HEAD, и отпечаток обязан это видеть."""
        state = {"verdicts": [{"role": "pi"}]}
        before = statecache.fingerprint(state, git_snapshot=lambda: "head-abc")
        after = statecache.fingerprint(state, git_snapshot=lambda: "head-xyz")
        self.assertNotEqual(before, after)

    def test_missing_snapshot_callable_is_not_an_error(self) -> None:
        """Нет ни снимка, ни вердиктов — отпечаток всё равно считается."""
        self.assertTrue(statecache.fingerprint({"evidence": []}))


class TestUnchangedAnswer(unittest.TestCase):
    def test_shape_and_hash(self) -> None:
        answer = statecache.unchanged_answer("abc123")
        self.assertTrue(answer["unchanged"])
        self.assertEqual(answer["hash"], "abc123")

    def test_note_points_at_a_tool_that_works(self) -> None:
        """Заглушка отсылает к `pair_digest`, поэтому он обязан быть вызываем.

        Инструкция в навыке `pair-work` начинает раунд именно с него, и кэш
        отсылает туда же: если дайджест не отвечает, экономия обещана тому, кто
        её не получает, и агент молча уходит платить за полный `pair_state`.
        """
        note = statecache.unchanged_answer("h")["note"]
        self.assertIn("pair_digest", note)

        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from server import HANDLERS, tool_definitions

        advertised = {tool["name"] for tool in tool_definitions()}
        self.assertIn("pair_digest", advertised)
        self.assertTrue(callable(HANDLERS.get("pair_digest")),
                        "pair_digest объявлен, но не вызываем")


class TestCacheOverARealSession(PairTestCase):
    """Отпечаток на настоящей сессии, а не на словаре.

    Юнит-тесты выше доказывают, что отпечаток реагирует на перечисленные поля.
    Эти доказывают, что он реагирует на то, что агент видит между двумя вызовами
    `pair_state` в живом раунде. Именно на них падал старый ключ:
    `obligations` и `evidence_this_round` не попадали в отпечаток, и второй вызов
    возвращал заглушку — то есть агент, записавший доказательство, читал его
    отсутствие.

    Право записи берётся по-настоящему: доказательство, записанное без него,
    не evidence этого раунда, и проверка прошла бы на пустом месте.
    """

    def setUp(self) -> None:
        super().setUp()
        self.writer.tool_pair_state()
        self.peer.tool_pair_state()
        self.writer.tool_pair_claim(reason="writer")
        for phase in ("plan", "implement", "verify"):
            self.writer.tool_pair_advance(phase)

    def test_evidence_becomes_visible_on_the_next_call(self) -> None:
        """Агент должен видеть причину заранее, а не тратить ход на заведомый отказ."""
        self.set_autonomy("semi")
        state = self.writer.tool_pair_state()
        self.assertTrue(state["closing_blocked_by"])
        self.assertTrue(state["awaiting_human_ok"])
        self.assertEqual(state["evidence_this_round"], [])

        self.record_test_evidence()

        state = self.writer.tool_pair_state()
        self.assertEqual(
            state["evidence_this_round"][0]["kind"], "test",
            "заглушка вернулась: записанное доказательство не видно",
        )
        self.assertEqual(len(state["closing_blocked_by"]), 1,
                         "осталось только слово человека")

    def test_a_new_obligation_is_visible_to_both_sides(self) -> None:
        """Обязательства лежат на диске, и отпечаток обязан их видеть.

        Иначе автор письма читал «никто никого не ждёт» о том, что он только что
        попросил сосада о проверке, и отправлял второе письмо в тот же ящик.
        """
        self.writer.tool_pair_send(to="reviewer-agent", text="проверь",
                                   expects_reply=True)

        mine = self.writer.tool_pair_state()["obligations"]
        self.assertEqual(1, len(mine["i_am_waiting_for"]))
        self.assertEqual("reviewer-agent", mine["i_am_waiting_for"][0]["to"])

        theirs = self.peer.tool_pair_state()["obligations"]
        self.assertEqual(1, len(theirs["they_wait_for_me"]))
        self.assertEqual([], theirs["i_am_waiting_for"])

    def test_a_repeated_call_with_nothing_changed_is_a_stub(self) -> None:
        """Сам кэш тоже должен работать, иначе он платит за то, чего не делает.

        Общая фикстура выключает кэш намеренно — остальные наборы проверяют
        контракт «каждый зов возвращает полное состояние». Здесь контракт
        обратный, поэтому кэш включается на время теста и снимается обратно
        tearDown, который вернёт переменную как была.

        Проверяем обе стороны: неизменившееся состояние даёт короткий ответ, а
        изменившееся — полный. Без второй половины тест проходил бы и при
        кэше, который всегда молчит.
        """
        os.environ.pop(statecache.CACHE_DISABLED_ENV, None)
        try:
            first = self.writer.tool_pair_state()
            self.assertNotIn("unchanged", first)

            second = self.writer.tool_pair_state()
            self.assertTrue(second.get("unchanged"), "кэш не сработал на неизменившемся")
            self.assertIn("pair_digest", second["note"])

            self.record_test_evidence()
            self.assertNotIn(
                "unchanged", self.writer.tool_pair_state(),
                "кэш ответил заглушкой после записи доказательства",
            )
        finally:
            os.environ[statecache.CACHE_DISABLED_ENV] = "1"


if __name__ == "__main__":
    unittest.main(verbosity=2)