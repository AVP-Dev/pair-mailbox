"""Тесты живости соседа: прочитает ли он новое сообщение.

Один тест-файл на один исходник: `liveness.py` отвечает за одно — что видно
каждому участнику про остальных, — и проверяется здесь.

Проверка `alive_check` подменяется везде, где речь о смерти процесса: тест не
должен зависеть от того, жив ли PID в момент прогона. Один тест на реальный
вызов оставлен намеренно — иначе подмена прятала бы опечатку в самой проверке.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import liveness  # noqa: E402


def state_with(**kwargs) -> dict:
    base = {"phase": "implement", "roles": {}}
    base.update(kwargs)
    return base


def dead(_pid) -> bool:
    return False


def alive(_pid) -> bool:
    return True


class TestPidAlive(unittest.TestCase):
    def test_own_pid_is_alive(self) -> None:
        """Единственный тест на настоящий вызов: подмена прятала бы опечатку."""
        self.assertTrue(liveness.pid_alive(os.getpid()))

    def test_missing_pid_is_not_alive(self) -> None:
        self.assertFalse(liveness.pid_alive(None))

    def test_zero_is_not_alive(self) -> None:
        self.assertFalse(liveness.pid_alive(0))

    def test_negative_pid_is_not_alive(self) -> None:
        self.assertFalse(liveness.pid_alive(-1))

    def test_dead_pid_is_not_alive(self) -> None:
        self.assertFalse(liveness.pid_alive(99999999))

    def test_garbage_pid_does_not_raise(self) -> None:
        """Роль в ростере может прийти с чем угодно вместо pid.

        Исключение здесь уронило бы `pair_state` целиком — то есть агент не
        узнал бы ничего ни о ком, из-за чужой испорченной записи.
        """
        for bad in ("не число", "", object(), 3.5):
            self.assertFalse(liveness.pid_alive(bad), bad)

    def test_bool_is_an_int_in_python_and_survives_as_alive(self) -> None:
        """`True` проходит проверку `isinstance(pid, int)` — так было и раньше.

        Зафиксировано, а не исправлено: это поведение `server.is_pid_alive`,
        перенесённое дословно, и менять его здесь — значит менять протокол мимо
        задачи. Значение в ростере не может быть `True`: там pid или число, или
        ничего. Тест назван прямо, чтобы правка когда-нибудь не выглядела
        случайной.
        """
        self.assertTrue(liveness.pid_alive(True))

    def test_eperm_means_alive_not_dead(self) -> None:
        """Процесс чужой — значит живой, просто не наш.

        Сосед может быть запущен от другого пользователя. Объявить его мёртвым —
        значит не ждать ответа, который придёт, и писать следующий раунд в пустоту.
        """
        import errno as _errno
        import unittest.mock as _mock

        with _mock.patch.object(liveness.os, "kill",
                               side_effect=OSError(_errno.EPERM, "not yours")):
            self.assertTrue(liveness.pid_alive(os.getpid()))

    def test_other_oserror_means_dead(self) -> None:
        import errno as _errno
        import unittest.mock as _mock

        with _mock.patch.object(liveness.os, "kill",
                               side_effect=OSError(_errno.ESRCH, "no such process")):
            self.assertFalse(liveness.pid_alive(os.getpid()))


class TestPeerStatusShape(unittest.TestCase):
    def test_self_is_absent(self) -> None:
        """Свою роль в списке соседей видеть нечего: это не сосед."""
        status = liveness.peer_status(
            state_with(roles={"opencode": {"pid": 1}, "pi": {"pid": 2}}),
            role="opencode", writer="opencode", alive_check=alive,
        )
        self.assertEqual(list(status), ["pi"])

    def test_roles_are_labelled_against_the_writer(self) -> None:
        """Кто писатель, задаёт раскладку, а не факт регистрации в ростере."""
        status = liveness.peer_status(
            state_with(roles={"pi": {"pid": 2}, "agy": {"pid": 3}}),
            role="opencode", writer="pi", alive_check=alive,
        )
        self.assertEqual(status["pi"]["role"], "writer")
        self.assertEqual(status["agy"]["role"], "critic")

    def test_no_peers_gives_an_empty_mapping(self) -> None:
        status = liveness.peer_status(state_with(), role="opencode", writer=None)
        self.assertEqual(status, {})


class TestWillReadNewMessages(unittest.TestCase):
    def test_live_peer_in_an_open_round_reads(self) -> None:
        status = liveness.peer_status(
            state_with(roles={"pi": {"pid": 2}}), role="opencode",
            writer="opencode", alive_check=alive,
        )
        peer = status["pi"]
        self.assertTrue(peer["alive"])
        self.assertTrue(peer["will_read_new_messages"])
        self.assertEqual(peer["hint"], "Listening.")

    def test_dead_peer_does_not_read_and_is_named(self) -> None:
        """Молчание соседа должно быть сказано вслух, с его pid.

        Иначе письмо уходит в закрытый раунд, и находка не приходит никогда.
        """
        status = liveness.peer_status(
            state_with(roles={"pi": {"pid": 4242}}), role="opencode",
            writer="opencode", alive_check=dead,
        )
        peer = status["pi"]
        self.assertFalse(peer["alive"])
        self.assertFalse(peer["will_read_new_messages"])
        self.assertIn("4242", peer["hint"])

    def test_closed_round_beats_a_live_process(self) -> None:
        """Процесс жив, но раунд закрыт — новое он не прочитает.

        Обратный порядок проверок врал бы в другую сторону: живой pid выглядел
        бы как готовый сосед, и письмо ушло бы в никуда без единого слова.
        """
        status = liveness.peer_status(
            state_with(phase="done", roles={"pi": {"pid": 2}}),
            role="opencode", writer="opencode", alive_check=alive,
        )
        peer = status["pi"]
        self.assertTrue(peer["round_closed"])
        self.assertFalse(peer["will_read_new_messages"])
        self.assertIn("closed", peer["hint"].lower())

    def test_hint_names_the_way_back(self) -> None:
        """Подсказка обязана называть поступок, а не только констатировать факт."""
        status = liveness.peer_status(
            state_with(phase="done", roles={"pi": {"pid": 2}}),
            role="opencode", writer="opencode", alive_check=alive,
        )
        self.assertIn("pair_advance", status["pi"]["hint"])


class TestSilentSeconds(unittest.TestCase):
    def test_counted_from_last_seen(self) -> None:
        status = liveness.peer_status(
            state_with(roles={"pi": {"pid": 2, "last_seen": "2026-01-01T00:00:00+00:00"}}),
            role="opencode", writer="opencode", alive_check=alive,
        )
        self.assertGreater(status["pi"]["silent_seconds"], 0)

    def test_missing_last_seen_is_none_not_a_crash(self) -> None:
        status = liveness.peer_status(
            state_with(roles={"pi": {"pid": 2}}), role="opencode",
            writer="opencode", alive_check=alive,
        )
        self.assertIsNone(status["pi"]["silent_seconds"])

    def test_unparseable_last_seen_is_none_not_a_crash(self) -> None:
        """Испорченный timestamp — повод не считать возраст, а не уронить ответ."""
        status = liveness.peer_status(
            state_with(roles={"pi": {"pid": 2, "last_seen": "вчера"}}),
            role="opencode", writer="opencode", alive_check=alive,
        )
        self.assertIsNone(status["pi"]["silent_seconds"])
        self.assertIn("pi", status)


class TestUnreadCounters(unittest.TestCase):
    """P1: затык виден за секунды. Счётчик unread по ролям — в peers."""

    def test_unread_arrives_with_the_count(self) -> None:
        status = liveness.peer_status(
            state_with(roles={"pi": {"pid": 2}}), role="opencode",
            writer="opencode", alive_check=alive, unread={"pi": 3},
        )
        self.assertEqual(3, status["pi"]["unread"])

    def test_no_count_means_no_field_not_zero(self) -> None:
        """Ноль означал бы «прочитано всё», а отсутствие данных — неизвестно."""
        status = liveness.peer_status(
            state_with(roles={"pi": {"pid": 2}}), role="opencode",
            writer="opencode", alive_check=alive,
        )
        self.assertNotIn("unread", status["pi"])



if __name__ == "__main__":
    unittest.main(verbosity=2)