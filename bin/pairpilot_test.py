#!/usr/bin/env python3
"""Тесты сверхвизора: проверяют что он ДЕЛАЕТ, а не что файл существует.

Каждый тест поднимает своё состояние в tmp и смотрит на результат: появилось ли
отложенное сообщение, записано ли событие в транскрипт, написан ли отчёт.

Запуск:  python3 bin/pairpilot_test.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "mcp" / "pair_mailbox"))

_spec = importlib.util.spec_from_loader("pairpilot", None)
pairpilot = importlib.util.module_from_spec(_spec)
pairpilot.__dict__["__file__"] = str(ROOT / "bin" / "pairpilot")
exec(compile((ROOT / "bin" / "pairpilot").read_text(encoding="utf-8"), "pairpilot", "exec"), pairpilot.__dict__)


def now_iso() -> str:
    """Текущий момент в том же формате, что пишет сервер в `last_seen`.

    Отдельная функция вместо правки состояния руками: сравнение идёт по строке, и
    «не совпало» здесь означает «роль не проснулась». Ручной литерал был бы
    верным только сегодня.
    """
    return datetime.now(timezone.utc).isoformat()


def ago(seconds: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


class PilotCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.tmp.name) / ".pair"
        self.state_dir.mkdir(parents=True)
        # TMUX_PANE убираем на время теста и возвращаем после. В test_protocol.py
        # это было сделано из-за F-08 (набор не должен слать реальный спам в окно
        # оператора), а здесь — по другой и более неприятной причине: тесты
        # обязательств используют панель %20, и при запуске набора ИЗ %20 пилот
        # считает, что agy сидит в его собственной панели, добавляет её в skip и
        # молчит. Набор падал два теста в зависимости от того, из какой панели его
        # запустили, — то есть проверка зависела от места, а не от кода.
        self._orig_tmux_pane = os.environ.pop("TMUX_PANE", None)

    def tearDown(self) -> None:
        if self._orig_tmux_pane is not None:
            os.environ["TMUX_PANE"] = self._orig_tmux_pane
        else:
            os.environ.pop("TMUX_PANE", None)
        self.tmp.cleanup()

    def pilot(self, silence: float = 600.0) -> "pairpilot.Pilot":
        # tmux выключаем: тесты не должны дёргать живые окна.
        # Профили тоже изолируем: динамический `launch_command` читает настоящий
        # `cli-profiles.json`, и тест «нет команды — нет перезапуска» падал бы
        # на живом дереве, где профиль `agy` есть.
        p = pairpilot.Pilot(self.state_dir, silence=silence, interval=1)
        p.tmux = None
        orig = p._launch_commands
        here = self.state_dir
        def isolated(given=None):
            saved = pairpilot.ROOT
            try:
                pairpilot.ROOT = here
                return orig(given)
            finally:
                pairpilot.ROOT = saved
        p._launch_commands = isolated
        return p

    def write_state(self, **fields) -> dict:
        state = {
            "task": "тест",
            "mode": "fix",
            "writer": None,
            "phase": "implement",
            "round": 1,
            "roles": {},
            "seen": {},
            "patches": {},
            # Явный пустой список: состояние говорит на новом языке, и старая
            # строка task не поднимается обратно открытой задачей. Иначе каждый
            # тест этого файла получал бы блокер «задача не сделана».
            "tasks": [],
        }
        state.update(fields)
        (self.state_dir / "state.json").write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        return state

    def wake(self, pilot: "pairpilot.Pilot", role: str) -> dict:
        """Записанное пробуждение роли, или {} если его нет."""
        path = pilot.wake_root / f"{role}.json"
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def assert_enqueued_for_push(self, pilot: "pairpilot.Pilot", role: str) -> None:
        """Почтальон толкает сразу: очередь НЕ пуста после непрочитанного.

        Старый контракт требовал тишины до эскалации (wake-файл, опрос через
        4 мин, потом печать) — спящий агент узнавал о письме через минуты,
        и пара работала только пока человек толкал вручную. Новый контракт:
        память (wake) и толчок (outbox) пишутся вместе, flush этого же тика
        вбивает толчок в свободный композер.
        """
        self.assertTrue(pilot.pending(role),
                        "непрочитанное обязано лечь в очередь толчка сразу")

    def inbox(self, role: str, count: int) -> None:
        path = self.state_dir / "mailbox" / role
        path.mkdir(parents=True, exist_ok=True)
        for index in range(count):
            (path / f"00000{index}-x.json").write_text("{}", encoding="utf-8")

    def transcript_kinds(self) -> list[str]:
        path = self.state_dir / "transcript.jsonl"
        if not path.exists():
            return []
        return [json.loads(line)["kind"] for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def read_state(self) -> dict:
        """Состояние с диска, а не из памяти пилота.

        Проверять надо, что записано в ростер, а не что вернул последний mutate:
        иначе утверждение про обновление pid проверяло бы сам же тест.
        """
        return json.loads((self.state_dir / "state.json").read_text(encoding="utf-8"))

    def transcript_entries(self) -> list[dict]:
        path = self.state_dir / "transcript.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class TestWakeFileIntegrity(PilotCase):
    """Пробуждение — один файл на роль, и он не теряет то, что уже накопил.

    Поводов о пробуждении два (`unread_peers` и `stale_writer`), а файл один.
    Перезапись выглядела безобидно, пока не посчитано: она обнуляла `asked_at`,
    то есть терпение начиналось заново, и стирала `escalated`, то есть один и тот
    же факт эскалировался дважды — по первому поводу, а потом уже по второму.
    """

    def test_since_seconds_is_measured_before_the_file_is_removed(self) -> None:
        """`since_seconds` обязан что-то значить.

        Порядок был перепутан: `unlink` шёл до `stat`, а `exists()` после удаления
        всегда `False`, поэтому поле всегда было 0 — то есть по нему нельзя было
        понять, сколько роль молчала. Здесь файл намеренно состарен, иначе
        округление до целых секунд скрыло бы ошибку нуля.
        """
        self.write_state(writer="agy", roles={"agy": {"last_seen": ago(3600), "pid": 1, "pane": "%1"}})
        pilot = self.pilot()
        pilot.tick()
        path = pilot.wake_root / "agy.json"
        self.assertTrue(path.exists())
        old = time.time() - 600
        os.utime(path, (old, old))
        # Роль отвечает: last_seen сдвинулся, пробуждение снимается.
        self.write_state(writer="agy", roles={"agy": {"last_seen": now_iso(), "pid": 1, "pane": "%1"}})
        pilot.tick()
        entries = [e for e in self.transcript_entries()
                   if e.get("kind") == "pilot_wake_acknowledged"]
        self.assertEqual(1, len(entries))
        self.assertGreaterEqual(
            entries[0]["since_seconds"], 590,
            "молчание измеряется до удаления файла, иначе поле всегда ноль")

    def test_two_reasons_escalate_once_not_twice(self) -> None:
        """Два повода о пробуждении — одна эскалация.

        Проверка на семантику, а не на список вызовов: поднять `wake_grace` в ноль
        мало, нужно чтобы оба повода сработали в одном раунде на одну роль.
        """
        self.inbox("agy", 3)
        self.write_state(writer="agy", roles={"agy": {"last_seen": ago(3600), "pid": 1, "pane": "%1"}})
        pilot = self.pilot()
        pilot.wake_grace = 0.0
        pilot.tick()
        pilot.tick()
        pilot.tick()
        kinds = [e.get("kind") for e in self.transcript_entries()]
        self.assertGreaterEqual(
            kinds.count("pilot_wake_escalated"), 1,
            "хотя бы одна эскалация обязана быть")
        self.assertEqual(
            1, kinds.count("pilot_wake_escalated"),
            "непрочитанные и молчание писателя — один факт пробуждения, "
            "а не два")

    def test_a_pending_wake_keeps_its_first_moment(self) -> None:
        """`asked_at` — момент постановки вопроса, а не последней записи.

        Обновление счётчика с того момента, когда роль уже должна была проснуться,
        делает «терпение» бесконечным: вопрос, на который не ответили, снова
        получает полный срок ожидания.
        """
        # `last_seen` держим ОДНОЙ строкой на весь тест: `ago()` даёт разные
        # микросекунды в каждом вызове, и роль получается «проснувшейся» сама
        # собой — то есть проверяла бы не то.
        quiet = ago(3600)
        self.write_state(writer="agy", roles={"agy": {"last_seen": quiet, "pid": 1, "pane": "%1"}})
        pilot = self.pilot()
        pilot.tick()
        first = self.wake(pilot, "agy")
        self.assertTrue(first)
        first_asked = first["asked_at"]
        # Второй повод на ту же роль, без её ответа.
        self.inbox("agy", 2)
        pilot.tick()
        second = self.wake(pilot, "agy")
        self.assertTrue(second, "второй повод не должен стирать висящее пробуждение")
        self.assertEqual(first_asked, second["asked_at"],
                         "срок терпения считается от первого вопроса")
        self.assertIn("непрочитанных", " ".join(second["reasons"]),
                      "второй повод обязан быть виден в том же файле")


class TestLiveness(PilotCase):
    def test_silent_writer_is_woken_silently(self) -> None:
        """Молчащий писатель: пробуждение записано, в композер не напечатано.

        Проверка стала строже прежней. Раньше она требовала, чтобы напоминание
        оказалось в очереди печати, — то есть чтобы сверхвизор полез в чужое
        окно. Теперь требуется обратное: факт есть, а в чужом окне пусто. Если
        правка напечатала бы, тест упал бы, а не стал бы зеленее.
        """
        self.write_state(writer="agy", roles={"agy": {"last_seen": ago(3600), "pid": 1, "pane": "%1"}})
        pilot = self.pilot()
        pilot.tick()
        wake = self.wake(pilot, "agy")
        self.assertTrue(wake, "молчащий писатель должен получить пробуждение")
        self.assertIn("фазу", wake["reason"])
        self.assertIn("право записи", wake["text"])
        self.assert_enqueued_for_push(pilot, "agy")

    def test_a_writer_who_woke_up_is_not_printed_at(self) -> None:
        """Проснувшийся не будится второй раз — и об этом есть запись.

        Раньше это было неотличимо от «доставили и забыли»: очередь печати
        очищалась и после успеха, и после отброшенного по сроку. Теперь исход
        виден в журнале, и по нему можно проверить, что печать не понадобилась.
        """
        self.write_state(writer="agy", roles={"agy": {"last_seen": ago(3600), "pid": 1, "pane": "%1"}})
        pilot = self.pilot()
        pilot.tick()
        self.assertTrue(self.wake(pilot, "agy"))
        # Роль отвечает: last_seen сдвинулся.
        self.write_state(writer="agy", roles={"agy": {"last_seen": now_iso(), "pid": 1, "pane": "%1"}})
        pilot.tick()
        self.assertEqual({}, self.wake(pilot, "agy"), "пробуждение снято")
        kinds = [e.get("kind") for e in self.transcript_entries()]
        self.assertIn("pilot_wake_acknowledged", kinds)
        self.assert_enqueued_for_push(pilot, "agy")

    def test_a_writer_who_ignores_the_wake_is_printed_at_once(self) -> None:
        """Эскалация: печать становится исключением, а не первым шагом.

        Порядок здесь и есть содержание правки. Если бы печать шла сразу, роль
        с длинной правкой получала бы тычок в композер на каждом тике; теперь она
        сначала получает шанс проснуться сама, и печать приходит один раз и
        поздно — когда молчание уже становится риском для раунда.
        """
        self.write_state(writer="agy", roles={"agy": {"last_seen": ago(3600), "pid": 1, "pane": "%1"}})
        pilot = self.pilot()
        pilot.wake_grace = 0.0
        pilot.tick()
        pilot.tick()
        printed = pilot.pending("agy")
        self.assertTrue(printed, "после истечения терпения печать обязана быть")
        self.assertIn("право записи", printed)
        pilot.tick()
        pilot.tick()
        self.assertEqual(printed, pilot.pending("agy"),
                         "эскалация печатает один раз, а не каждый тик")
        kinds = [e.get("kind") for e in self.transcript_entries()]
        self.assertEqual(1, kinds.count("pilot_wake_escalated"),
                         "эскалация не должна повторяться на каждом тике")

    def test_escalation_repeats_after_a_long_silence(self) -> None:
        """Эскалация обязана повторяться.

        Прежний контракт был «ровно один раз за жизнь роли», и он оказался
        ловушкой без выхода: файл удаляется только когда роль ПРОСНУЛАСЬ, поэтому
        у молчающей роли вторая эскалация невозможна в принципе. Найдено вживую:
        pi молчал час, файл висел с escalated, супервизор не печатал.
        """
        self.write_state(writer="agy", roles={"agy": {"last_seen": ago(3600), "pid": 1, "pane": "%1"}})
        pilot = self.pilot()
        pilot.wake_grace = 0.0
        pilot.tick()
        pilot.tick()
        self.assertTrue(pilot.pending("agy"), "первая эскалация обязана быть")
        # Очередь очищается: иначе следующая проверка увидит не свою эскалацию, а
        # подталкивание по простою писателя — другой повод и другой текст.
        # Проверяется именно повтор пробуждения.
        pilot.outbox_file("agy").unlink(missing_ok=True)

        # Пауза между повторами ещё не прошла — тикаем часто, повтора быть не должно.
        for _ in range(5):
            pilot.tick()
        self.assertEqual("", pilot.pending("agy"),
                         "в пределах паузы повторять нельзя, это шум")

        # Пауза прошла, роль не проснулась — повтор обязан быть.
        wake_file = pilot.wake_root / "agy.json"
        payload = json.loads(wake_file.read_text(encoding="utf-8"))
        payload["escalated_at"] = ago(int(pairpilot.WAKE_ESCALATE_REPEAT_SECONDS) + 60)
        wake_file.write_text(json.dumps(payload), encoding="utf-8")

        pilot.tick()
        self.assertTrue(pilot.pending("agy"),
                        "роль молчит дольше паузы — повторная эскалация обязана быть")
        kinds = [e.get("kind") for e in self.transcript_entries()]
        self.assertGreaterEqual(kinds.count("pilot_wake_escalated"), 2,
                                "повтор должен попасть в транскрипт")
        repeats = [e for e in self.transcript_entries()
                   if e.get("kind") == "pilot_wake_escalated" and e.get("repeat")]
        self.assertEqual(1, len(repeats), "повтор помечен в транскрипте отдельно")

    def test_outbox_ttl_outlives_the_escalation_pause(self) -> None:
        """TTL очереди обязан быть длиннее паузы между эскалациями.

        Это не вкусовое предпочтение, а условие работоспособности. При TTL 600 и
        паузе 900 эскалация протухала в очереди раньше, чем повтор в неё
        попадал: пилот писал «выброшено, ждало композер 15 мин», и повторяться
        было нечему. Найдено на живой сессии — повторная эскалация не дошла до
        композера ни разу.

        Проверяется сравнением констант, а не поведением, потому что поведение
        здесь и есть следствие: сначала падает доставка, и заметить можно
        поздно.
        """
        self.assertGreater(
            pairpilot.DEFAULT_OUTBOX_TTL, pairpilot.WAKE_ESCALATE_REPEAT_SECONDS,
            "TTL очереди короче паузы между эскалациями: повтор не доедет никогда")

    def test_escalation_survives_long_enough_to_repeat(self) -> None:
        """Эскалация должна пережить собственный срок ожидания.

        Проверка на живом пути: эскалация кладёт текст в очередь, и очередь не
        должна выбросить его раньше, чем повтор.
        """
        self.write_state(writer="agy", roles={"agy": {"last_seen": ago(3600), "pid": 1, "pane": "%1"}})
        pilot = self.pilot()
        pilot.wake_grace = 0.0
        pilot.tick()
        pilot.tick()
        path = pilot.outbox_file("agy")
        self.assertTrue(path.exists(), "эскалация должна лечь в очередь")
        age = time.time() - path.stat().st_mtime
        self.assertLess(age, pilot.outbox_ttl,
                        "эскалация не должна протухать в собственной очереди")

    def test_fresh_writer_is_left_alone(self) -> None:
        self.write_state(writer="agy", roles={"agy": {"last_seen": ago(30), "pane": "%1"}})
        pilot = self.pilot()
        pilot.tick()
        self.assertEqual({}, self.wake(pilot, "agy"), "молчание в пределах нормы не трогаем")

    def test_nudge_is_not_queued_when_round_is_done(self) -> None:
        self.write_state(writer="agy", phase="done", closed_at=ago(10),
                         roles={"agy": {"last_seen": ago(3600), "pane": "%1"}})
        pilot = self.pilot()
        pilot.tick()
        self.assertEqual("", pilot.pending("agy"), "закрытый раунд не будим")

    def test_silent_role_with_no_input_is_nudged_once(self) -> None:
        """Молчащая роль без входящих в живом раунде — отсутствие связи.

        Раньше здесь стоял обратный пин: «молчание критика — не дефект,
        возможно, ему нечего сказать». Замер 2026-10-11 показал, чем это
        кончается: в 2 раундах из 8 писатель не писал соседу до смены фазы,
        и критика не будил никто — у пилота не было для него пути. Прежний
        контракт удалён, новый: порог молчания, который был только у
        писателя (`stale_writer`), симметрично применяется к роли без
        входящих. Толчок один на раунд, повод — `pilot_idle_role`.
        """
        self.write_state(writer="agy", roles={"agy": {"last_seen": ago(30), "pane": "%1"},
                                              "codex": {"last_seen": ago(3600), "pane": "%2"}})
        pilot = self.pilot()
        pilot.tick()
        self.assertTrue(self.wake(pilot, "codex"), "молчащую роль без входящих будим")
        self.assertTrue(pilot.pending("codex"), "толчок сразу, как у непрочитанного")
        self.assertIn("pilot_idle_role", self.transcript_kinds())
        pilot.tick()
        self.assertEqual(len([k for k in self.transcript_kinds()
                              if k == "pilot_idle_role"]), 1, "повтор в том же раунде — нет")

    def test_fresh_role_is_left_alone(self) -> None:
        self.write_state(writer="agy", roles={"agy": {"last_seen": ago(30), "pane": "%1"},
                                              "codex": {"last_seen": ago(60), "pane": "%2"}})
        pilot = self.pilot()
        pilot.tick()
        self.assertEqual({}, self.wake(pilot, "codex"), "молчание в пределах нормы не трогаем")

    def test_role_nudge_is_not_queued_when_round_is_done(self) -> None:
        self.write_state(writer="agy", phase="done", closed_at=ago(10),
                         roles={"agy": {"last_seen": ago(30), "pane": "%1"},
                                "codex": {"last_seen": ago(3600), "pane": "%2"}})
        pilot = self.pilot()
        pilot.tick()
        self.assertEqual("", pilot.pending("codex"), "закрытый раунд не будим")

    def test_role_with_unread_stays_with_the_mail_path(self) -> None:
        """Почта будит свой путь; второй толчок тем же текстом — спам."""
        self.inbox("codex", 2)
        self.write_state(writer="agy", roles={"codex": {"last_seen": ago(3600), "pane": "%1"}},
                         seen={"codex": 0})
        pilot = self.pilot()
        pilot.tick()
        self.assertIn("pilot_unread_nudge", self.transcript_kinds())
        self.assertNotIn("pilot_idle_role", self.transcript_kinds())

    def test_unread_nudge_is_sent_once_and_not_every_tick(self) -> None:
        """Напоминание о непрочитанном — толчок сразу, но один раз: агент
        заканчивает ход сам (PROTOCOL §7), и толчок не должен превращаться в
        поток, который сам и будит, и засыпает."""
        self.inbox("codex", 2)
        self.write_state(writer="agy", roles={"codex": {"last_seen": ago(60), "pane": "%1"}},
                         seen={"codex": 0})
        pilot = self.pilot()
        pilot.tick()
        self.assertTrue(self.wake(pilot, "codex"))
        pilot.tick()
        pilot.tick()
        # Считаем события в журнале, а не текст: текст напоминания одинаковый
        # каждый раз, и сравнение строк повтор не видит вовсе.
        nudges = [e for e in self.transcript_entries() if e.get("kind") == "pilot_unread_nudge"]
        self.assertEqual(1, len(nudges),
                         "напоминание не должно повторяться каждый тик")
        self.assert_enqueued_for_push(pilot, "codex")

    def test_unread_nudge_does_not_touch_a_role_with_nothing_unread(self) -> None:
        """Напоминание без письма — шум, а помощь."""
        self.write_state(writer="agy", roles={"codex": {"last_seen": ago(60), "pane": "%1"}},
                         seen={"codex": 0})
        pilot = self.pilot()
        pilot.tick()
        self.assertEqual("", pilot.pending("codex"))

    def test_second_letter_after_reading_wakes_again(self) -> None:
        """Прочитал — штамп сброшен: второе письмо будит заново.

        Регрессия раунда 43: штамп unread-{role} жил вечно, и второе письмо
        агента уже не будило никогда.
        """
        self.inbox("codex", 2)
        self.write_state(writer="agy", roles={"codex": {"last_seen": ago(60), "pane": "%1"}},
                         seen={"codex": 0})
        pilot = self.pilot()
        pilot.tick()
        first = [e for e in self.transcript_entries() if e.get("kind") == "pilot_unread_nudge"]
        self.assertEqual(1, len(first))
        # Агент прочитал всё: ящик пуст, seen сброшен — как после pair_read.
        for f in (self.state_dir / "mailbox" / "codex").glob("*.json"):
            f.unlink()
        self.write_state(writer="agy", roles={"codex": {"last_seen": ago(60), "pane": "%1"}},
                         seen={"codex": 0})
        pilot.tick()
        # Пришло новое письмо — штамп уже сброшен, будит заново.
        self.inbox("codex", 1)
        pilot.tick()
        nudges = [e for e in self.transcript_entries() if e.get("kind") == "pilot_unread_nudge"]
        self.assertEqual(2, len(nudges),
                         "второе письмо после прочтения обязано будить заново")

    def test_enqueue_replacement_leaves_a_trace(self) -> None:
        """Вытеснение из очереди пишется в журнал, а не исчезает молча."""
        self.write_state(writer="agy", roles={"codex": {"last_seen": ago(60), "pane": "%1"}})
        pilot = self.pilot()
        pilot.enqueue("codex", "первое напоминание")
        pilot.enqueue("codex", "второе напоминание")
        replaced = [e for e in self.transcript_entries()
                    if e.get("kind") == "pilot_outbox_replaced"]
        self.assertEqual(1, len(replaced))
        self.assertIn("первое", replaced[0].get("old", ""))
        self.assertEqual("второе напоминание", pilot.pending("codex"))

    def test_hanging_request_repeats_after_repeat_interval(self) -> None:
        """Зависший запрос напоминает снова, но не чаще раза в reply_repeat."""
        roles = {"codex": {"last_seen": ago(60), "pane": "%1"},
                 "agy": {"last_seen": ago(60), "pane": "%2"}}
        self.write_state(writer="agy", roles=roles)
        pilot = self.pilot()
        pilot.reply_grace = 0
        pilot.reply_repeat = 3600.0
        root = self.state_dir / "obligations"
        root.mkdir(parents=True, exist_ok=True)
        (root / "000007-q.json").write_text(json.dumps({
            "seq": 7, "from": "agy", "to": "codex",
            "opened_at": ago(120), "subject": "вопрос 7",
        }), encoding="utf-8")
        pilot.tick()
        first_text = pilot.pending("codex")
        self.assertIn("запрос 7", first_text,
                      "первое напоминание о зависшем запросе обязательно")
        pilot.tick()
        self.assertEqual(first_text, pilot.pending("codex"),
                         "повтор раньше reply_repeat запрещён")
        # Состариваем штамп за предел повтора — напоминание повторяется.
        stamp = pilot.state_dir / "pilot" / f".{pairpilot.stamp_name('reply-7-codex')}"
        old = time.time() - 3700.0
        os.utime(stamp, (old, old))
        pilot.tick()
        self.assertIn("уже 2 мин", pilot.pending("codex"),
                      "просроченный штамп обязан дать повторное напоминание")


class TestUnread(PilotCase):
    def test_unread_mail_produces_a_wake_and_a_push(self) -> None:
        """Письмо есть — память и толчок есть: wake-файл и очередь вбивки.

        Старый контракт требовал тишины до эскалации — спящий агент узнавал
        о письме через минуты опроса, и пара работала только пока человек
        толкал вручную. Новый контракт почтальона: толчок сразу, память
        рядом, flush этого же тика вбивает его в свободный композер.
        """
        self.inbox("agy", 3)
        self.write_state(roles={"agy": {"last_seen": ago(60), "pane": "%1"}}, seen={"agy": 0})
        pilot = self.pilot()
        pilot.tick()
        wake = self.wake(pilot, "agy")
        self.assertTrue(wake, "непрочитанное письмо обязано оставить след")
        self.assertIn("непрочитанных", wake["reason"])
        self.assertIn("непрочитанных", wake["text"])
        self.assertEqual(3, wake["unread_at_ask"], "сколько именно не прочитано")
        self.assert_enqueued_for_push(pilot, "agy")

    def test_read_mail_is_not_reminded(self) -> None:
        self.inbox("agy", 3)
        self.write_state(roles={"agy": {"last_seen": ago(60), "pane": "%1"}}, seen={"agy": 3})
        pilot = self.pilot()
        pilot.tick()
        self.assertEqual("", pilot.pending("agy"))


class TestPaneAmbiguity(PilotCase):
    def test_roles_sharing_a_pane_are_never_touched(self) -> None:
        """Две роли на одной панели — ростер врёт. Бить в неё нельзя: «будим
        claude» уйдёт в окно agy."""
        self.inbox("claude", 5)
        self.write_state(writer="agy", seen={},
                         roles={"agy": {"last_seen": ago(60), "pane": "%20"},
                                "claude": {"last_seen": ago(60), "pane": "%20"}})
        pilot = self.pilot()
        pilot.tick()
        self.assertEqual("", pilot.pending("claude"), "в общую панель не тыкаем")
        self.assertIn("pilot_pane_shared", self.transcript_kinds())


class TestOutboxStaleness(PilotCase):
    """Отложенное напоминание не должно переживать вопрос.

    Наблюдалось вживую: напоминание про обязательство, закрытое почти за три
    часа до составления, лежало в очереди ещё 22 минуты и было доставлено как
    свежее — человек получил вопрос, который уже не имел отношения к делу.
    Причина в коде: `flush_outbox` при ComposerBusy делал `continue` и повторял
    вечно, а файл удалялся только при успешной доставке. Срока жизни не было
    вовсе, и «переживающая доставка» незаметно стала доставкой протухшего.
    """

    def _queue(self, pilot, role: str, text: str, age_seconds: float = 0.0) -> Path:
        pilot.enqueue(role, text)
        path = pilot.outbox / f"{role}.txt"
        when = time.time() - age_seconds
        os.utime(path, (when, when))
        return path

    def test_fresh_nudge_is_delivered(self) -> None:
        p = self.pilot()
        path = self._queue(p, "agy", "свежее напоминание")
        state = self.write_state(roles={"agy": {"pane": "%1", "pid": os.getpid()}})
        orig_alive, orig_deliver = pairpilot.pane_alive, pairpilot.deliver
        pairpilot.pane_alive = lambda tmux, pane: True
        pairpilot.deliver = lambda pane, text, wait_for_empty=0.0: {"delivered": True}
        try:
            p.tmux = "tmux"
            p.flush_outbox(state)
        finally:
            pairpilot.pane_alive, pairpilot.deliver = orig_alive, orig_deliver
        self.assertFalse(path.exists(), "свежее напоминание доставляется и убирается")

    def test_stale_nudge_is_dropped_and_never_delivered(self) -> None:
        """Вопрос устарел — доставить его хуже, чем не доставить."""
        p = self.pilot()
        p.outbox_ttl = 600.0
        path = self._queue(p, "agy", "вопрос двухчасовой давности", age_seconds=7200)
        state = self.write_state(roles={"agy": {"pane": "%1", "pid": os.getpid()}})
        calls = []
        orig_alive, orig_deliver = pairpilot.pane_alive, pairpilot.deliver
        pairpilot.pane_alive = lambda tmux, pane: True
        pairpilot.deliver = lambda pane, text, wait_for_empty=0.0: calls.append(text) or {"delivered": True}
        try:
            p.tmux = "tmux"
            p.flush_outbox(state)
        finally:
            pairpilot.pane_alive, pairpilot.deliver = orig_alive, orig_deliver
        self.assertEqual(calls, [], "протухшее не доставляется даже при свободном композере")
        self.assertFalse(path.exists(), "протухшее убирается из очереди")
        kinds = [e.get("kind") for e in p.store.read_transcript(limit=0)]
        self.assertIn("pilot_nudge_expired", kinds,
                      "выброшенное должно быть видно в транскрипте, иначе выглядит как доставленное")

    def test_expired_nudge_falls_back_to_mailbox(self) -> None:
        """Протухшее в композере дублируется в ящик, а не исчезает.

        Измерено 2026-10-06: два nudge_expired подряд, оба агента живы,
        оба ничего не получили. Указатель протухал в outbox, пока агент
        работал и композер был занят. Mailbox не протухает: агент
        заберёт письмо через pair_read, когда сам решит.
        """
        p = self.pilot()
        p.outbox_ttl = 600.0
        path = self._queue(p, "agy", "прочитай pair_read", age_seconds=7200)
        state = self.write_state(roles={"agy": {"pane": "%1", "pid": os.getpid()}})
        orig_alive = pairpilot.pane_alive
        pairpilot.pane_alive = lambda tmux, pane: True
        try:
            p.tmux = "tmux"
            p.flush_outbox(state)
        finally:
            pairpilot.pane_alive = orig_alive
        inbox = list((self.state_dir / "mailbox" / "agy").glob("*.json"))
        self.assertEqual(1, len(inbox), "указатель обязан лежать в ящике")
        body = json.loads(inbox[0].read_text(encoding="utf-8"))
        self.assertEqual("pairpilot", body["from"])
        self.assertIn("pair_read", body["text"])
        kinds = [e.get("kind") for e in p.store.read_transcript(limit=0)]
        self.assertIn("pilot_mailbox_fallback", kinds)

    def test_a_busy_composer_does_not_expire_a_fresh_nudge(self) -> None:
        """Переживающая доставка остаётся: пока композер занят, ждать уместно."""
        p = self.pilot()
        p.outbox_ttl = 600.0
        path = self._queue(p, "agy", "подождём", age_seconds=60)
        state = self.write_state(roles={"agy": {"pane": "%1", "pid": os.getpid()}})

        def busy(pane, text, wait_for_empty=0.0):
            raise pairpilot.ComposerBusy("занято")

        orig_alive, orig_deliver = pairpilot.pane_alive, pairpilot.deliver
        pairpilot.pane_alive = lambda tmux, pane: True
        pairpilot.deliver = busy
        try:
            p.tmux = "tmux"
            p.flush_outbox(state)
        finally:
            pairpilot.pane_alive, pairpilot.deliver = orig_alive, orig_deliver
        self.assertTrue(path.exists(), "свежее напоминание переживает занятый композер")


class TestReport(PilotCase):
    def test_closed_round_produces_a_report(self) -> None:
        self.write_state(phase="done", closed_at=ago(60), round=3, writer="agy",
                         roles={"agy": {"last_seen": ago(60), "pane": "%1"}})
        (self.state_dir / "transcript.jsonl").write_text(
            json.dumps({"kind": "deliver", "role": "agy"}) + "\n"
            + json.dumps({"kind": "deliver_busy", "role": "opencode"}) + "\n",
            encoding="utf-8")
        pilot = self.pilot()
        pilot.tick()
        report = self.state_dir / "REPORT.md"
        self.assertTrue(report.exists(), "закрытый раунд обязан оставить отчёт")
        text = report.read_text(encoding="utf-8")
        self.assertIn("тест", text)
        self.assertIn("deliver_busy", text)
        self.assertIn("НЕ доказывает", text)
        self.assertIn("pilot_report", self.transcript_kinds())

    def test_open_round_writes_nothing(self) -> None:
        self.write_state(phase="verify", roles={"agy": {"last_seen": ago(10), "pane": "%1"}})
        pilot = self.pilot()
        pilot.tick()
        self.assertFalse((self.state_dir / "REPORT.md").exists())

    def test_report_is_written_once(self) -> None:
        self.write_state(phase="done", closed_at=ago(60), round=3,
                         roles={"agy": {"last_seen": ago(60), "pane": "%1"}})
        pilot = self.pilot()
        pilot.tick()
        first = (self.state_dir / "REPORT.md").stat().st_mtime_ns
        pilot.tick()
        self.assertEqual(first, (self.state_dir / "REPORT.md").stat().st_mtime_ns,
                         "отчёт не переписывается каждый проход")


class TestSafety(PilotCase):
    def test_pilot_never_touches_write_authority(self) -> None:
        """Граница, ради которой сверхвизор и сверхвизор: он не имеет права
        выдать или забрать авторитет, даже если агент молчит."""
        self.write_state(writer="agy", roles={"agy": {"last_seen": ago(9999), "pane": "%1"}})
        pilot = self.pilot()
        pilot.tick()
        state = json.loads((self.state_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual("agy", state.get("writer"))
        self.assertNotIn("assign", {k for k in dir(pairpilot.Pilot) if "assign" in k})

    def test_pilot_never_moves_the_phase(self) -> None:
        self.write_state(writer="agy", phase="verify",
                         roles={"agy": {"last_seen": ago(9999), "pane": "%1"}})
        pilot = self.pilot()
        pilot.tick()
        state = json.loads((self.state_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual("verify", state.get("phase"))

    def test_silence_threshold_is_honoured(self) -> None:
        self.write_state(writer="agy", roles={"agy": {"last_seen": ago(400), "pane": "%1"}})
        tolerant = self.pilot(silence=600)
        tolerant.tick()
        self.assertEqual("", tolerant.pending("agy"), "400с молчания при пороге 600с — норма")

    def test_short_silence_threshold_wakes(self) -> None:
        self.write_state(writer="agy", roles={"agy": {"last_seen": ago(400), "pane": "%1"}})
        strict = self.pilot(silence=100)
        strict.tick()
        self.assertTrue(self.wake(strict, "agy"),
                        "400с молчания при пороге 100с — дефект")


class TestTmuxPaths(PilotCase):
    """Пути, которые зовут tmux. Им нужна подмена, иначе они проверяются
    только в бою — а именно там падал append_transcript с чужим role=."""

    def fake_tmux(self, alive=True, prompt=None, verdict="allow"):
        pilot = self.pilot()
        pilot.tmux = "/usr/bin/true"
        pairpilot.pane_alive = lambda tmux, pane: alive
        pairpilot.resolve_permission_prompt = (
            lambda tmux, pane, allow=None: {"approved": verdict == "allow",
                                            "verdict": verdict,
                                            "prompt": prompt or "",
                                            "why": "в белом списке" if verdict == "allow" else "не в белом списке",
                                            "matched": None})
        return pilot

    def tearDown(self) -> None:
        pairpilot.pane_alive = self._real_alive
        pairpilot.resolve_permission_prompt = self._real_prompt
        pairpilot.subprocess = self._real_subprocess
        super().tearDown()

    def setUp(self) -> None:
        self._real_alive = getattr(pairpilot, "pane_alive", None)
        self._real_prompt = getattr(pairpilot, "resolve_permission_prompt", None)
        self._real_subprocess = getattr(pairpilot, "subprocess", None)
        super().setUp()

    def test_missing_pane_is_recorded_without_crashing(self) -> None:
        self.write_state(roles={"codex": {"last_seen": ago(10)}})
        pilot = self.fake_tmux()
        pilot.tick()
        self.assertIn("pilot_pane_missing", self.transcript_kinds())

    def test_live_pid_without_pane_heals_the_roster(self) -> None:
        """Панели нет в ростере, но процесс роли жив и сидит в панели:
        чиним запись, а не хороним роль. Иначе deliver слеп при живом соседе."""
        import os
        self.write_state(roles={"codex": {"last_seen": ago(10), "pid": os.getpid()}})
        pilot = self.fake_tmux()
        real_find = pairpilot.find_pane_of_pid
        pairpilot.find_pane_of_pid = lambda tmux, pid: "%7"
        try:
            pilot.tick()
        finally:
            pairpilot.find_pane_of_pid = real_find
        self.assertIn("pilot_pane_healed", self.transcript_kinds())
        self.assertEqual(self.pilot().store.read()["roles"]["codex"]["pane"], "%7")

    def test_live_project_pane_heals_roster_without_pid(self) -> None:
        """pid мёртв/пуст, но в проекте есть живая свободная панель: чиним
        запись по cwd, а не хороним роль (P1, критик seq 381)."""
        self.write_state(roles={"codex": {"last_seen": ago(10), "pid": 1}})
        pilot = self.fake_tmux()
        real_find = pairpilot.find_pane_of_pid
        real_belongs = pairpilot.pane_belongs_to_project
        real_rolepid = pairpilot.pane_role_pid
        real_run = pairpilot.subprocess.run
        pairpilot.find_pane_of_pid = lambda tmux, pid: ""
        pairpilot.pane_belongs_to_project = (
            lambda tmux, pane, root: (pane == "%11", ""))
        pairpilot.pane_role_pid = lambda tmux, pane, role: 4242 if pane == "%11" else 0
        pairpilot.subprocess.run = lambda *a, **k: type("R", (), {
            "stdout": "%9\n%11\n", "returncode": 0})()
        pilot.pane_key = lambda pane: str(pane)
        try:
            pilot.tick()
        finally:
            pairpilot.find_pane_of_pid = real_find
            pairpilot.pane_belongs_to_project = real_belongs
            pairpilot.pane_role_pid = real_rolepid
            pairpilot.subprocess.run = real_run
        self.assertIn("pilot_pane_healed", self.transcript_kinds())
        self.assertEqual(self.pilot().store.read()["roles"]["codex"]["pane"], "%11")

    def test_foreign_pane_does_not_heal_the_roster(self) -> None:
        """Чужая панель не чинит ростер: принадлежность не доказана —
        роль остаётся без панели, fail-closed."""
        self.write_state(roles={"codex": {"last_seen": ago(10), "pid": 1}})
        pilot = self.fake_tmux()
        real_find = pairpilot.find_pane_of_pid
        real_belongs = pairpilot.pane_belongs_to_project
        real_run = pairpilot.subprocess.run
        pairpilot.find_pane_of_pid = lambda tmux, pid: ""
        pairpilot.pane_belongs_to_project = lambda tmux, pane, root: (False, "чужой")
        pairpilot.subprocess.run = lambda *a, **k: type("R", (), {"stdout": "%9\n", "returncode": 0})()
        pilot.pane_key = lambda pane: str(pane)
        try:
            pilot.tick()
        finally:
            pairpilot.find_pane_of_pid = real_find
            pairpilot.pane_belongs_to_project = real_belongs
            pairpilot.subprocess.run = real_run
        self.assertIn("pilot_pane_missing", self.transcript_kinds())
        self.assertNotIn("pane", self.pilot().store.read()["roles"]["codex"])

    def test_human_shell_is_not_adopted_without_role_server(self) -> None:
        """Панель проекта без server.py роли (шелл человека) не усыновляется:
        cwd второй фактор, первый — живой процесс роли (вердикт раунда 33)."""
        self.write_state(roles={"codex": {"last_seen": ago(10), "pid": 1}})
        pilot = self.fake_tmux()
        real_find = pairpilot.find_pane_of_pid
        real_belongs = pairpilot.pane_belongs_to_project
        real_rolepid = pairpilot.pane_role_pid
        real_run = pairpilot.subprocess.run
        pairpilot.find_pane_of_pid = lambda tmux, pid: ""
        pairpilot.pane_belongs_to_project = lambda tmux, pane, root: (True, "")
        pairpilot.pane_role_pid = lambda tmux, pane, role: 0
        pairpilot.subprocess.run = lambda *a, **k: type("R", (), {"stdout": "%9\n", "returncode": 0})()
        pilot.pane_key = lambda pane: str(pane)
        try:
            pilot.tick()
        finally:
            pairpilot.find_pane_of_pid = real_find
            pairpilot.pane_belongs_to_project = real_belongs
            pairpilot.pane_role_pid = real_rolepid
            pairpilot.subprocess.run = real_run
        self.assertIn("pilot_pane_missing", self.transcript_kinds())
        self.assertNotIn("pane", self.pilot().store.read()["roles"]["codex"])

    def test_provisional_pane_is_preferred_but_rechecked(self) -> None:
        """Жизненный цикл pane_provisional: сервер видел панель, пилот
        ставит её первой, но факторы перепроверяет — без server.py
        не усыновляет даже её (вердикт раунда 33, seq 392)."""
        self.write_state(roles={"codex": {"last_seen": ago(10), "pid": 1,
                                          "pane_provisional": "%11"}})
        pilot = self.fake_tmux()
        real_belongs = pairpilot.pane_belongs_to_project
        real_rolepid = pairpilot.pane_role_pid
        real_run = pairpilot.subprocess.run
        pairpilot.pane_belongs_to_project = lambda tmux, pane, root: (True, "")
        pairpilot.pane_role_pid = lambda tmux, pane, role: 4242
        pairpilot.subprocess.run = lambda *a, **k: type("R", (), {
            "stdout": "%9\n%11\n", "returncode": 0})()
        pilot.pane_key = lambda pane: str(pane)
        try:
            found = pilot.live_project_panes(self.pilot().store.read(), "codex")
        finally:
            pairpilot.pane_belongs_to_project = real_belongs
            pairpilot.pane_role_pid = real_rolepid
            pairpilot.subprocess.run = real_run
        self.assertEqual("%11", found[0], "provisional — первая в очереди")

    def test_wake_placement_rings_the_pane_without_typing(self) -> None:
        """Постановка wake звонит в панель display-message, а не печатает
        в композер: звонок виден, строка ввода цела."""
        self.write_state(roles={"codex": {"last_seen": ago(10), "pane": "%9"}})
        pilot = self.fake_tmux()
        calls = []
        real_run = pairpilot.subprocess.run
        pairpilot.subprocess.run = lambda *a, **k: calls.append(a[0]) or type("R", (), {
            "stdout": "", "returncode": 0})()
        try:
            pilot.mark_wake(self.pilot().store.read(), "codex", "сообщение", "текст")
        finally:
            pairpilot.subprocess.run = real_run
        self.assertIn("pilot_ring", self.transcript_kinds())
        self.assertTrue(any("display-message" in c for c in calls),
                        "звонок идёт через display-message, а не send-keys")
        self.assertFalse(any("send-keys" in c for c in calls),
                         "печать в композер запрещена")

    def test_ring_is_throttled_to_five_minutes(self) -> None:
        """Второй звонок подряд — молчание: чаще раза в 5 мин не звоним."""
        self.write_state(roles={"codex": {"last_seen": ago(10), "pane": "%9"}})
        pilot = self.fake_tmux()
        real_run = pairpilot.subprocess.run
        pairpilot.subprocess.run = lambda *a, **k: type("R", (), {
            "stdout": "", "returncode": 0})()
        try:
            state = self.pilot().store.read()
            pilot.mark_wake(state, "codex", "сообщение", "текст")
            before = self.transcript_kinds().count("pilot_ring")
            pilot.mark_wake(self.pilot().store.read(), "codex", "сообщение", "текст")
            after = self.transcript_kinds().count("pilot_ring")
        finally:
            pairpilot.subprocess.run = real_run
        self.assertEqual(before, after, "повторный звонок в пределах 5 мин запрещён")

    def test_dead_pane_is_recorded_once(self) -> None:
        self.write_state(roles={"agy": {"last_seen": ago(10), "pane": "%9"}})
        pilot = self.fake_tmux(alive=False)
        pilot.tick()
        pilot.tick()
        self.assertEqual(1, self.transcript_kinds().count("pilot_pane_dead"),
                         "о мёртвой панели говорим один раз, а не каждые 15 секунд")

    def test_role_without_wake_adapter_is_named_not_silent(self) -> None:
        """Нет адаптера — запись в журнал, а не молчание (pi молчал 3 часа)."""
        self.write_state(roles={"pi": {"last_seen": ago(10), "pane": "%9"}})
        self.fake_tmux().mark_wake(self.pilot().store.read(), "pi", "сообщение", "текст")
        self.assertIn("pilot_wake_no_adapter", self.transcript_kinds())

    def test_permission_prompt_is_confirmed_and_logged(self) -> None:
        self.write_state(roles={"agy": {"last_seen": ago(10), "pane": "%9"}})
        pilot = self.fake_tmux(prompt="Allow Read of server.py?")
        pilot.tick()
        self.assertIn("pilot_unblock", self.transcript_kinds())

    def test_refused_permission_is_recorded_not_pressed(self) -> None:
        """Отказ — это результат. Молча не нажать «y» нельзя: человек должен
        знать, что панель стоит и почему он должен встать к ней сам."""
        self.write_state(roles={"agy": {"last_seen": ago(10), "pane": "%9"}})
        pilot = self.fake_tmux(prompt="Allow execution of bash? rm -rf /", verdict="refuse")
        pilot.tick()
        self.assertIn("pilot_unblock_refused", self.transcript_kinds())
        self.assertNotIn("pilot_unblock", self.transcript_kinds())

    def test_observe_mode_never_confirms_a_prompt(self) -> None:
        self.write_state(autonomy="observe", roles={"agy": {"last_seen": ago(10), "pane": "%9"}})
        pilot = self.fake_tmux(prompt="Allow Read of server.py?")
        pilot.tick()
        self.assertNotIn("pilot_unblock", self.transcript_kinds(),
                         "в observe человек за рулём, сверхвизор только говорит")

    def test_live_pane_produces_no_panic(self) -> None:
        self.write_state(writer="agy", roles={"agy": {"last_seen": ago(10), "pane": "%9"}})
        pilot = self.fake_tmux()
        pilot.tick()
        self.assertNotIn("pilot_pane_dead", self.transcript_kinds())


class TestRestart(PilotCase):
    """Перезапуск упавшего агента. Это чинит ровно ту ночную поломку, когда pid
    GONE, а запись в ростере живёт часами."""

    DEAD_PID = 999999

    def setUp(self) -> None:
        self._real_subprocess = getattr(pairpilot, "subprocess", None)
        self.calls: list[list[str]] = []
        pairpilot.subprocess = _FakeSubprocess(self.calls)
        # Модульные подмены восстанавливаются здесь, а не в каждом тесте.
        # Проверено, что это не теория: подмена pane_pid из одного теста доживала
        # до следующих и делала их результат случайным — два теста падали, пока
        # подмена не вернулась после утечки. Тест, зависящий от порядка запуска,
        # проверяет не своё поведение.
        self._real_pane_alive = pairpilot.pane_alive
        self._real_pane_role_pid = getattr(pairpilot, "pane_role_pid", None)
        pairpilot.pane_role_pid = lambda tmux, pane, role: None
        # deliver печатает в живой композер через tmux send-keys: без мока
        # тесты слали спам в реальную панель %7 (раунд 42, жалоба agy).
        self._real_deliver = pairpilot.deliver
        pairpilot.deliver = lambda pane, text, wait_for_empty=0.0: {"delivered": True}
        super().setUp()

    def tearDown(self) -> None:
        pairpilot.subprocess = self._real_subprocess
        pairpilot.pane_alive = self._real_pane_alive
        pairpilot.deliver = self._real_deliver
        if self._real_pane_role_pid is not None:
            pairpilot.pane_role_pid = self._real_pane_role_pid
        super().tearDown()

    def dead_writer_state(self, **extra) -> None:
        self.write_state(writer="agy", round=3,
                         roles={"agy": {"last_seen": ago(600), "pid": self.DEAD_PID, "pane": "%999999"}},
                         **extra)

    def pilot_with_launch(self, command: str = "agy --resume"):
        pilot = self.pilot()
        pilot.tmux = "/usr/bin/tmux"
        pairpilot.pane_alive = lambda tmux, pane: True
        pilot._explicit_launch = {"agy": command} if command else {}
        return pilot

    def respawns(self, role: str | None = None) -> list[list[str]]:
        """Вызовы `respawn-pane`, и только они.

        Тесты ниже утверждают смысл — «роль перезапущена» или «роль НЕ
        перезапущена», — а не список команд. Разница не вкусовая: `pane_alive` и
        `pane_identity` ходят в tmux сами, поэтому сверхвизоку обоснованно
        понадобится ещё один `display-message` там, где раньше не было ни одного.
        Тест, требующий `self.calls == []`, падал бы на любой такой зов, и чинить
        это можно было только одним способом — не делать проверку. Поэтому здесь
        отсеиваются именно перезапуски, а количество и порядок прочих вызовов не
        утверждаются вовсе.
        """
        out = [call for call in self.calls if "respawn-pane" in call]
        if role is not None:
            out = [call for call in out if role in " ".join(call)]
        return out

    def assertNoRestart(self, why: str) -> None:
        self.assertEqual([], self.respawns(), why)

    def test_the_restart_never_kills_whats_in_the_pane(self) -> None:
        """`-k` был единственным, что разрешало убить процесс человека.

        Без него tmux отказывается перезапускать живую панель, поэтому занятую
        панель — шелл или отладчик, открытый человеком после падения агента, —
        остаётся нетронутой, а отказ попадает в транскрипт как отказ."""
        self.dead_writer_state()
        pilot = self.pilot_with_launch()
        pilot.tick()
        sent = self.respawns()
        self.assertTrue(sent)
        for call in sent:
            self.assertNotIn("-k", call, "перезапуск не должен убивать содержимое панели")

    def test_a_dead_role_does_not_make_a_pane_shared(self) -> None:
        """Живой дефект: claude и codex держались в ростере с панелью agy, и
        `skip` содержал всех троих — сверхвизор переставал обслуживать живого
        соседа из-за двух умерших записей."""
        self.write_state(writer="agy", round=3, roles={
            "agy": {"last_seen": ago(10), "pid": os.getpid(), "pane": "%7"},
            "claude": {"last_seen": ago(9000), "pid": 999999, "pane": "%7"},
        })
        pilot = self.pilot()
        pilot.tmux = "/usr/bin/tmux"
        pilot.tick()
        self.assertNotIn("agy", pilot.skip, "живой сосед не должен попадать под skip")
        self.assertNotIn("pilot_pane_shared", self.transcript_kinds())
        self.assertIn("pilot_stale_role", self.transcript_kinds())

    def test_a_stale_role_is_reported_once_not_every_tick(self) -> None:
        self.write_state(writer="agy", roles={
            "agy": {"last_seen": ago(10), "pid": os.getpid(), "pane": "%7"},
            "codex": {"last_seen": ago(9000), "pid": 999999, "pane": "%7"},
        })
        pilot = self.pilot()
        pilot.tick()
        pilot.tick()
        pilot.tick()
        stale = [k for k in self.transcript_kinds() if k == "pilot_stale_role"]
        self.assertEqual(1, len(stale), "об умершей роли говорим один раз, а не каждый тик")

    def test_dead_writer_is_respawned(self) -> None:
        self.dead_writer_state()
        pilot = self.pilot_with_launch()
        pilot.tick()
        # Утверждается факт перезапуска в нужной панели с нужной командой, а не
        # то, каким по счёту был вызов: `self.calls[0]` — это утверждение о
        # порядке, и оно ломается от любой правки в соседнем коде.
        self.assertEqual([["/usr/bin/tmux", "respawn-pane", "-t", "%999999", "agy --resume"]],
                         self.respawns(),
                         "упавший писатель перезапускается в своей панели")
        self.assertIn("pilot_restart", self.transcript_kinds())

    def test_restart_happens_once_per_dead_pid(self) -> None:
        self.dead_writer_state()
        pilot = self.pilot_with_launch()
        pilot.tick()
        pilot.tick()
        self.assertEqual(1, len(self.respawns()),
                         "один умерший процесс — один перезапуск, а не каждые 15 секунд")

    def test_dying_on_start_stops_being_restarted_forever(self) -> None:
        """Дыра, найденная на ревью.

        Ключ по pid спасает от повторного перезапуска одного и того же упавшего
        процесса — и ровно поэтому не спасает от другого: если команда запуска
        неверна, процесс умирает с НОВЫМ pid, ключ каждый раз новый, и при
        интервале 15 секунд это бесконечный цикл. Потолок в окне не зависит от
        pid, поэтому именно он его останавливает."""
        pilot = self.pilot_with_launch()
        for attempt in range(6):
            # каждая неудачная попытка даёт новый pid, как и было бы в бою
            self.write_state(writer="agy", round=3,
                             roles={"agy": {"last_seen": ago(600), "pid": 999000 + attempt,
                                            "pane": "%7"}})
            pilot.tick()
        respawns = self.respawns()
        self.assertEqual(pilot.restart_limit, len(respawns),
                         f"перезапусков должно быть не больше {pilot.restart_limit}, "
                         f"а их {len(respawns)}")
        self.assertIn("pilot_restart_throttled", self.transcript_kinds())

    def test_restart_budget_expires_with_the_window(self) -> None:
        """Потолок не должен становиться permanent: иначе после сбоя сеть
        отвалилась бы пара насовсем."""
        pilot = self.pilot_with_launch()
        pilot.restart_window = 60.0
        for attempt in range(pilot.restart_limit + 2):
            self.write_state(writer="agy", round=3,
                             roles={"agy": {"last_seen": ago(600), "pid": 998000 + attempt,
                                            "pane": "%7"}})
            pilot.tick()
        self.assertEqual(pilot.restart_limit, len(self.respawns()))
        # Окно истекло: попытки вне окна не должны считаться.
        pilot.restarts_file("agy").write_text(
            " ".join(str(int(time.time() - 3600)) for _ in range(pilot.restart_limit)),
            encoding="utf-8")
        self.write_state(writer="agy", round=3,
                         roles={"agy": {"last_seen": ago(600), "pid": 998111, "pane": "%7"}})
        pilot.tick()
        self.assertEqual(pilot.restart_limit + 1,
                         len(self.respawns()))
    def test_launch_profile_added_after_start_is_picked_up(self) -> None:
        """Профиль, добавленный после старта пилота, обязан подхватиться.

        Регрессия 2026-10-06: пилот от 05.10 не знал команду `pi` и писал
        `pilot_restart_impossible`, хотя `cli-profiles.json` уже содержал
        профиль. Карта команд морозилась в `__init__` и без перезапуска
        пилота не обновлялась.
        """
        self.dead_writer_state()
        pilot = self.pilot_with_launch(command="")
        pilot.tmux = "/usr/bin/tmux"
        pairpilot.pane_alive = lambda tmux, pane: True
        pilot.tick()
        self.assertNoRestart("профиля нет — перезапуска нет")
        self.assertIn("pilot_restart_impossible", self.transcript_kinds())
        pilot._explicit_launch["agy"] = "agy --resume"
        pilot.tick()
        self.assertEqual(1, len(self.respawns()),
                         "команда появилась позже старта — перезапуск обязан сработать")

    def test_budget_is_per_role_not_global(self) -> None:
        pilot = self.pilot_with_launch()
        pilot._explicit_launch["claude"] = "claude"
        for _ in range(pilot.restart_limit + 2):
            self.write_state(writer="agy", round=3,
                             roles={"agy": {"last_seen": ago(600), "pid": self.DEAD_PID,
                                            "pane": "%7"}})
            pilot.tick()
        self.write_state(writer="agy", round=3,
                         roles={"agy": {"last_seen": ago(600), "pid": self.DEAD_PID, "pane": "%7"},
                                "claude": {"last_seen": ago(600), "pid": self.DEAD_PID, "pane": "%8"}})
        pilot.tick()
        self.assertTrue(self.respawns("claude"),
                        "у другого агента свой бюджет")

    def test_live_process_is_never_respawned(self) -> None:
        """Молчание лечится напоминанием, смерть — перезапуском. Убить того,
        кто просто занят работой, — значит отнять у него контекст."""
        self.write_state(writer="agy",
                         roles={"agy": {"last_seen": ago(10), "pid": os.getpid(), "pane": "%7"}})
        pilot = self.pilot_with_launch()
        pilot.tick()
        self.assertNoRestart("живой процесс не перезапускается никогда")
        self.assertNotIn("pilot_restart", self.transcript_kinds())

    def test_a_live_process_in_the_pane_is_not_respawned(self) -> None:
        """Главный инцидент ночи: 21 попытка перезапустить панель, в которой
        агент ЖИВ.

        Роль перезапустилась сама, или просто сменился pid, а запись в ростере
        осталась с протухшим. Панель жива, в ней сидит агент — и `respawn-pane`
        с `-k` его убивал бы вместе со всем контекстом раунда.

        Проверяется ровно то, о чём был инцидент: перезапуска НЕТ, а pid в
        записи обновлён на тот, что реально жив. Порядок обращений к tmux здесь
        не утверждается никак.
        """
        self.dead_writer_state()
        pilot = self.pilot_with_launch()
        # Живой pid, и НЕ наш собственный: сам сверхвизор, запущенный из панели
        # роли, pid в её запись не пишет, и подмена os.getpid() проверяла бы не то.
        live = os.getppid()
        pairpilot.pane_role_pid = lambda tmux, pane, role: live
        pilot.tick()
        self.assertNoRestart("в панели живой процесс — перезапуск его бы убил")
        self.assertEqual(live, self.read_state()["roles"]["agy"]["pid"],
                         "pid в записи обязан стать тем, что реально жив")
        self.assertIn("pilot_pid_stale_pane_alive", self.transcript_kinds())
        entry = [e for e in self.transcript_entries()
                 if e["kind"] == "pilot_pid_stale_pane_alive"][-1]
        self.assertEqual(live, entry["live_pid"])
        self.assertEqual(self.DEAD_PID, entry["pid"],
                         "причина обязана называть протухший pid, а не текущий")

    def test_a_dead_process_in_the_pane_is_still_respawned(self) -> None:
        """Обратная сторона: живая ПАНЕЛЬ не значит живой АГЕНТ.

        Если бы `pane_pid` вёлся на «панель занята — значит всё в порядке», то
        падение агента при живом окне осталось бы незамеченным, а это ровно тот
        случай, ради которого перезапуск и написан. Поэтому проверка симметрична
        предыдущей и обязана существовать вместе с ней.
        """
        self.dead_writer_state()
        pilot = self.pilot_with_launch()
        pairpilot.pane_role_pid = lambda tmux, pane, role: self.DEAD_PID
        pilot.tick()
        self.assertEqual([["/usr/bin/tmux", "respawn-pane", "-t", "%999999", "agy --resume"]],
                         self.respawns(),
                         "в панели мёртвый pid — роль поднимается")
        self.assertNotIn("pilot_pid_stale_pane_alive", self.transcript_kinds())

    def test_an_unprovable_pane_pid_does_not_block_the_restart(self) -> None:
        """«Не знаю» не должно читаться как «занято».

        Если бы ноль от `pane_pid` трактовался как «панель занята», то после
        любой ошибки tmux сверхвизор навсегда перестал бы перезапускать упавших
        ролей — молча, без единой записи. Отказ должен быть громким и полезным.
        """
        self.dead_writer_state()
        pilot = self.pilot_with_launch()
        pairpilot.pane_role_pid = lambda tmux, pane, role: 0
        pilot.tick()
        self.assertEqual(1, len(self.respawns()),
                         "не доказано, что в панели кто-то есть, — перезапуск не запрещён")
        self.assertEqual(self.DEAD_PID, self.read_state()["roles"]["agy"]["pid"],
                         "запись без живого pid не выдумывается")

    def test_a_squatters_pid_never_lands_in_the_role_record(self) -> None:
        """Панель занята ЧУЖОЙ ролью — и в ней есть живой процесс.

        Это проверка ПОРЯДКА, а не наличия защиты. Если спросить `pane_pid` до
        проверки «панель занята другой ролью», вернётся живой pid СОСЕДА, и мы
        запишем его в запись этой роли. Две записи об одном процессе — и ростер
        больше не разберётся, кто где сидит.

        Мутация этого кода: вернуть `pane_occupant` перед `pane_occupied_by_other`
        — и тест краснеет. Это и было написано, чтобы поймать именно это.
        """
        self.write_state(writer="agy", round=3, roles={
            "agy": {"last_seen": ago(600), "pid": self.DEAD_PID, "pane": "%7"},
            "claude": {"last_seen": ago(30), "pid": os.getpid(), "pane": "%7"},
        })
        pilot = self.pilot_with_launch()
        pilot.pane_key = lambda pane: "%7"
        # В панели действительно кто-то жив — иначе проверка была бы пустой.
        pairpilot.pane_role_pid = lambda tmux, pane, role: os.getppid()
        pilot.tick()
        self.assertNoRestart("панель, занятая живой ролью, не трогаем")
        self.assertEqual(self.DEAD_PID, self.read_state()["roles"]["agy"]["pid"],
                         "pid соседа не должен оказаться в записи этой роли")
        self.assertNotIn("pilot_pid_stale_pane_alive", self.transcript_kinds(),
                         "занятая панель — это отказ, а не обновление pid")
        self.assertIn("pilot_restart_impossible", self.transcript_kinds())

    def test_a_live_shell_in_a_dead_roles_pane_is_not_mistaken_for_the_role(self) -> None:
        """Поломка, которую пропустили мои же тесты.

        Упавший агент оставляет в панели ЖИВОЙ шелл — так устроен терминал. Если
        «роль жива» выводить из «в панели есть процесс», то падавшая роль выглядит
        живой всегда, и перезапуск отменяется целиком. Это ровно тот случай, ради
        которого `restart_agent` написан, то есть правка живости по панели чинила
        перезапуск и ломала его.

        Измерено на этой машине: `#{pane_pid}` в панели с работающим агентом — это
        bash, а не агент. Поэтому спрашивать надо сервер роли, а не процесс панели.

        Здесь подменён сам источник pid так, как он ведёт себя в бою: панель занята
        живым шеллом, а процесса роли в ней нет. Ожидается перезапуск.
        """
        self.dead_writer_state()
        pilot = self.pilot_with_launch()
        # Живой посторонний процесс в панели — ровно то, что остаётся от агента.
        pairpilot.pane_role_pid = lambda tmux, pane, role: 0
        pilot.own_pane = None
        pilot.tick()
        self.assertEqual(1, len(self.respawns()),
                         "живой шелл в панели не доказывает, что роль жива — "
                         "иначе упавшая роль не перезапускалась бы никогда")

    def test_a_stale_pid_is_refreshed_once_not_every_tick(self) -> None:
        """Обновление — событие один раз, а не строчка в транскрипте каждые 15с."""
        self.dead_writer_state()
        pilot = self.pilot_with_launch()
        pairpilot.pane_role_pid = lambda tmux, pane, role: os.getppid()
        pilot.tick()
        pilot.tick()
        pilot.tick()
        stale = [k for k in self.transcript_kinds() if k == "pilot_pid_stale_pane_alive"]
        self.assertEqual(1, len(stale), "об устаревшем pid говорим один раз")

    def test_restart_needs_a_known_command(self) -> None:
        """Нет команды — нет перезапуска и громкая запись об этом. Выдуманная
        команда хуже отсутствующей: панель оживает и сразу падает."""
        self.dead_writer_state()
        pilot = self.pilot_with_launch(command="")
        pilot.tick()
        self.assertNoRestart("нет команды запуска — нет перезапуска")
        self.assertIn("pilot_restart_impossible", self.transcript_kinds())

    def test_dead_process_without_a_pane_is_left_to_the_human(self) -> None:
        """Окно, закрытое человеком, не поднимается молча: это не зависание,
        это решение."""
        self.write_state(writer="agy",
                         roles={"agy": {"last_seen": ago(600), "pid": self.DEAD_PID, "pane": "%7"}})
        pilot = self.pilot_with_launch()
        pairpilot.pane_alive = lambda tmux, pane: False
        pilot.tick()
        self.assertNoRestart("окно, закрытое человеком, не поднимается молча")
        self.assertIn("pilot_restart_impossible", self.transcript_kinds())

    def test_observe_mode_does_not_restart(self) -> None:
        self.dead_writer_state(autonomy="observe")
        pilot = self.pilot_with_launch()
        pilot.tick()
        self.assertNoRestart("в observe перезапуск — это действие, а не сообщение")

    def test_own_pane_is_never_respawned(self) -> None:
        """Пятый тормоз перезапуска: панель самого сверхвизора.

        `respawn-pane -k` убивает панель насовсем. Если это панель человека —
        там его ввод и сам сверхвизор: перезапуск роли убил бы оператора,
        который его запустил. Остальные пути (напоминание, разблокировка) эту
        панель берегут, и без проверки перезапуск остался бы единственным
        исключением из правила, которое везде записано словами.
        """
        self.dead_writer_state()
        pilot = self.pilot_with_launch()
        pilot.own_pane = "%999999"
        pilot.tick()
        self.assertNoRestart("свою панель не перезапускаем никогда")
        self.assertNotIn("pilot_restart", self.transcript_kinds())
        self.assertIn("pilot_own_pane", self.transcript_kinds())

    def test_role_sharing_a_pane_is_not_respawned(self) -> None:
        """Общая панель — это два агента в одном окне. Перезапуск одного из них
        убил бы второго вместе с его вводом, а из состояния не видно, кто где."""
        self.write_state(writer="agy", round=3, roles={
            "agy": {"last_seen": ago(600), "pid": self.DEAD_PID, "pane": "%7"},
            "claude": {"last_seen": ago(30), "pid": os.getpid(), "pane": "%7"},
        })
        pilot = self.pilot_with_launch()
        pilot.tick()
        self.assertNoRestart("панель, где сидит живой сосед, не трогаем")
        self.assertNotIn("pilot_restart", self.transcript_kinds())
        # Панель больше не «общая»: одна из двух записей принадлежит мёртвому
        # процессу, а мёртвая запись не делает окно занятым. Сообщается о
        # мёртвой роли и об отказе перезапуска, а не о двусмысленности.
        self.assertNotIn("pilot_pane_shared", self.transcript_kinds())
        self.assertIn("pilot_stale_role", self.transcript_kinds())
        impossible = [json.loads(line) for line in
                      (self.state_dir / "transcript.jsonl").read_text(encoding="utf-8").splitlines()
                      if line.strip() and json.loads(line).get("kind") == "pilot_restart_impossible"]
        self.assertIn("claude", impossible[-1]["why"])

    def test_a_squatter_recorded_in_the_other_format_is_still_a_squatter(self) -> None:
        """Тот же запрет, но сосед записан адресом сессии, а не `%N`.

        Панель, занятая живым соседом, перезапускать нельзя: `respawn-pane` убивает
        и его ввод. При сравнении строк сосед, записанный как `mail:0.1`, выглядел
        бы сидящим на ДРУГОЙ панели, панель сочлась бы свободной, и перезапуск
        снёс бы живого агента. Ровно та же поломка, что и с собственной панелью
        сверхвизора, только последствия здесь хуже.
        """
        self.write_state(writer="agy", round=3, roles={
            "agy": {"last_seen": ago(600), "pid": self.DEAD_PID, "pane": "%7"},
            "claude": {"last_seen": ago(30), "pid": os.getpid(), "pane": "mail:0.1"},
        })
        pilot = self.pilot_with_launch()
        # Настоящий ключ группировки, а не сравнение строк: здесь и проверяется
        # именно то, что оба формата приводятся к одному ключу.
        pilot.pane_key = lambda pane: {"%7": "mail:0.0", "mail:0.1": "mail:0.0"}.get(pane, pane)
        pilot.tick()
        self.assertNoRestart("панель, где сидит живой сосед, не трогаем")
        self.assertNotIn("pilot_restart", self.transcript_kinds())
        impossible = [json.loads(line) for line in
                      (self.state_dir / "transcript.jsonl").read_text(encoding="utf-8").splitlines()
                      if line.strip() and json.loads(line).get("kind") == "pilot_restart_impossible"]
        self.assertIn("claude", impossible[-1]["why"],
                      "отказ должен называть живого соседа, а не отсутствие панели")

    def test_failed_respawn_is_recorded_not_hidden(self) -> None:
        self.dead_writer_state()
        pilot = self.pilot_with_launch()
        self.calls.clear()
        pairpilot.subprocess = _FakeSubprocess(self.calls, returncode=1, stderr="can't find pane")
        pilot.tick()
        self.assertIn("pilot_restart_failed", self.transcript_kinds())


class _FakeSubprocess:
    """Подмена subprocess.run: пишет вызовы и возвращает заранее заданный код."""

    def __init__(self, calls: list[list[str]], returncode: int = 0, stderr: str = "") -> None:
        self.calls = calls
        self.returncode = returncode
        self.stderr = stderr
        self.run = self._run

    def _run(self, argv, **kwargs):
        self.calls.append(list(argv))
        return type("Result", (), {"returncode": self.returncode,
                                   "stdout": "", "stderr": self.stderr})()


class TestStalledPatches(PilotCase):
    """Sealed-патч, который никто не применил. Структурная дыра была в том, что
    `applied` не менялось никогда, поэтому «применён» и «висит с прошлой ночи»
    выглядели одинаково."""

    def sealed(self, sealed_age: int = 4000, applied: bool = False, owner: str = "codex") -> dict:
        patch = self.state_dir / "patches" / "pair-codex-1.patch"
        patch.parent.mkdir(parents=True, exist_ok=True)
        patch.write_text("--- a\n+++ b\n", encoding="utf-8")
        self.write_state(writer="agy", phase="verify", round=2,
                         roles={"agy": {"last_seen": ago(10), "pane": "%1"},
                                owner: {"last_seen": ago(10), "pane": "%2"}},
                         patches={str(patch): {"owner": owner, "sealed": True,
                                               "sealed_at": ago(sealed_age),
                                               "sha256": "deadbeef", "applied": applied}})
        return patch

    def pilot_with_grace(self, grace: float) -> "pairpilot.Pilot":
        pilot = self.pilot()
        pilot.patch_grace = grace
        return pilot

    def test_stalled_patch_wakes_the_writer_with_the_hash(self) -> None:
        self.sealed()
        pilot = self.pilot_with_grace(1800)
        pilot.tick()
        pending = pilot.pending("agy")
        self.assertIn("pair_patch_verify", pending)
        self.assertIn("deadbeef", pending)
        self.assertIn("pair_patch_applied", pending)

    def test_patch_nudge_applied_while_queued_is_dropped_not_delivered(self) -> None:
        """Протухшее в очереди напоминание про патч не доставляется (#18).

        Измерено 2026-10-06: `stalled_patches` положил текст в очередь до
        `patch_applied`, а `flush_outbox` доставил после — агент получил
        ложный «висит неприменённым» при applied=true.
        """
        self.sealed()
        pilot = self.pilot_with_grace(1800)
        pilot.tick()
        self.assertIn("pair_patch_verify", pilot.pending("agy"))
        self.sealed(applied=True)
        pilot.tick()
        self.assertEqual("", pilot.pending("agy"),
                         "патч применён, пока текст ждал, — доставлять нечего")
        self.assertIn("pilot_nudge_stale", self.transcript_kinds())

    def test_obligation_nudge_answered_while_queued_is_dropped_not_delivered(self) -> None:
        """Напоминание про закрытый запрос не доставляется.

        Измерено 2026-10-07 (backlog pilot-nudge-about-closed-obligation):
        напоминание про запрос 370 ушло через mailbox_fallback, хотя файл
        obligations/000370 уже был закрыт ответом. Текст про запрос/seq
        проверяется по файлу obligations/<seq>-*.json.
        """
        p = self.pilot()
        root = self.state_dir / "obligations"
        root.mkdir(parents=True, exist_ok=True)
        (root / "000370-q.json").write_text(json.dumps({
            "seq": 370, "from": "omp", "to": "agy",
            "opened_at": ago(60), "subject": "вопрос 370",
        }), encoding="utf-8")
        state = self.write_state(roles={"agy": {"pane": "%1", "pid": os.getpid()}})
        text = "omp ждёт ответа на запрос 370 уже 30 мин"
        self.assertTrue(p.nudge_still_true(state, "agy", text),
                        "запрос открыт — условие живо")
        (root / "000370-q.json").unlink()
        self.assertFalse(p.nudge_still_true(state, "agy", text),
                         "запрос закрыт, пока текст ждал, — доставлять нечего")


    def test_patched_owner_is_told_too(self) -> None:
        """Автор патча молчал и счёл его принятым. Его персональное дело —
        сказать, применён он или нет."""
        self.sealed()
        pilot = self.pilot_with_grace(1800)
        pilot.tick()
        self.assertIn("pair_patch_applied", pilot.pending("codex"))

    def test_fresh_patch_is_left_alone(self) -> None:
        self.sealed(sealed_age=60)
        self.pilot_with_grace(1800).tick()
        self.assertEqual("", self.pilot().pending("agy"), "минуту назад запечатали — это не дефект")

    def test_applied_patch_is_not_complained_about(self) -> None:
        self.sealed(applied=True)
        self.pilot_with_grace(1800).tick()
        self.assertEqual("", self.pilot().pending("agy"))

    def test_audit_round_does_not_chase_patches(self) -> None:
        self.sealed()
        self.write_state(mode="audit", writer="agy", phase="verify", round=2,
                         roles={"agy": {"last_seen": ago(10), "pane": "%1"},
                                "codex": {"last_seen": ago(10), "pane": "%2"}},
                         patches={str(self.state_dir / "patches" / "pair-codex-1.patch"):
                                  {"owner": "codex", "sealed": True, "sealed_at": ago(4000),
                                   "applied": False}})
        self.pilot_with_grace(1800).tick()
        self.assertEqual("", self.pilot().pending("agy"), "в аудите патчей не бывает вовсе")

    def test_nag_is_sent_once(self) -> None:
        self.sealed()
        pilot = self.pilot_with_grace(1800)
        pilot.tick()
        first = pilot.pending("agy")
        pilot.tick()
        self.assertEqual(first, pilot.pending("agy"), "одно и то же письмо дважды не отправляем")

    def test_stall_key_survives_a_path_with_slashes(self) -> None:
        """Ключ строится из пути патча. Если в имя отметки попадёт '/', remember()
        упадёт на первом же патче — и надзиратель упадёт вместе с ним."""
        self.sealed()
        pilot = self.pilot_with_grace(1800)
        pilot.tick()
        stamps = [p.name for p in (self.state_dir / "pilot").iterdir() if p.name.startswith(".stalled")]
        self.assertTrue(stamps, "отметка должна лежать плоско в pilot/, а не в каталогах")
        self.assertTrue(all("/" not in name for name in stamps))


class TestStamps(PilotCase):
    """Отметки должны переживать перезапуск сверхвизора, иначе он с утра
    «не помнит», что уже дёргал соседа, и пишет то же самое снова."""

    def test_stamp_name_is_stable_across_processes(self) -> None:
        """Встроенный hash() для строк случайный на каждый запуск Python."""
        script = (
            "import importlib.util, sys;"
            f"spec=importlib.util.spec_from_loader('p', None);"
            f"m=importlib.util.module_from_spec(spec);"
            f"m.__dict__['__file__']={str(ROOT / 'bin' / 'pairpilot')!r};"
            f"exec(compile(open({str(ROOT / 'bin' / 'pairpilot')!r}, encoding='utf-8').read(),"
            f" 'p', 'exec'), m.__dict__);"
            "print(m.stamp_name('unblock-agy-Do you want to proceed?'))"
        )
        first = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                               env=dict(os.environ, PYTHONHASHSEED="1"))
        second = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                                env=dict(os.environ, PYTHONHASHSEED="999"))
        self.assertTrue(first.returncode == 0, first.stderr)
        self.assertEqual(first.stdout.strip(), second.stdout.strip(),
                         "имя отметки не должно зависеть от PYTHONHASHSEED")

    def test_stamp_name_has_no_path_separators(self) -> None:
        name = pairpilot.stamp_name("stalled-/tmp/x/.pair/patches/pair-codex-1.patch-agy")
        self.assertNotIn("/", name)


class TestCheckpoint(PilotCase):
    """В semi закрыть раунд нельзя без слова человека. Значит человек должен
    получить всё для решения и команду, которой решает."""

    def verify_state(self, **extra) -> None:
        self.write_state(phase="verify", round=4, writer="agy",
                         roles={"agy": {"last_seen": ago(10), "pane": "%1"}}, **extra)

    def pilot_auto(self):
        pilot = self.pilot()
        pilot.autonomy = None
        return pilot

    def test_semi_without_ok_writes_a_digest(self) -> None:
        self.verify_state(autonomy="semi")
        pilot = self.pilot()
        pilot.tick()
        digest = self.state_dir / "pilot" / "checkpoint-4.md"
        self.assertTrue(digest.exists(), "человеку нужен дайджест, а не тишина")
        text = digest.read_text(encoding="utf-8")
        self.assertIn("bin/pairctl ok", text)
        self.assertIn("pair_evidence", text)
        self.assertIn("pilot_checkpoint", self.transcript_kinds())

    def test_semi_with_ok_writes_no_digest(self) -> None:
        # ok засчитывается только если сказано на verify/done, а прогон засчитывается
        # только от писателя — в фикстуре writer=agy, поэтому role тоже agy. Слово
        # без фазы и факт без роли — это не то же самое.
        self.verify_state(autonomy="semi", human_ok=[{"round": 4, "phase": "verify"}],
                          evidence=[{"round": 4, "kind": "test", "value": "117 passed",
                                     "ok": True, "role": "agy"}])
        pilot = self.pilot()
        pilot.tick()
        self.assertFalse((self.state_dir / "pilot" / "checkpoint-4.md").exists())

    def test_semi_with_ok_but_no_evidence_still_digests(self) -> None:
        """Слово человека получили, а доказательства нет — закрывать всё равно
        нечем, и дайджест должен называть именно это."""
        self.verify_state(autonomy="semi", human_ok=[{"round": 4, "phase": "verify"}])
        pilot = self.pilot()
        pilot.tick()
        text = (self.state_dir / "pilot" / "checkpoint-4.md").read_text(encoding="utf-8")
        self.assertIn("pair_evidence", text)
        self.assertNotIn("pairctl ok", text.split("## Что сделать")[0],
                         "слово человека уже есть, повторно просить его не нужно")

    def test_ok_given_on_plan_is_asked_for_again(self) -> None:
        """Одобрение плана не открывает закрытие раунда: человек должен увидеть
        результат и сказать «ок» уже на verify."""
        self.verify_state(autonomy="semi", human_ok=[{"round": 4, "phase": "plan"}],
                          evidence=[{"round": 4, "kind": "test", "value": "117 passed",
                                     "ok": True, "role": "agy"}])
        pilot = self.pilot()
        pilot.tick()
        text = (self.state_dir / "pilot" / "checkpoint-4.md").read_text(encoding="utf-8")
        self.assertIn("bin/pairctl ok", text,
                      "слово, сказанное не на том чекпоинте, надо услышать заново")

    def test_auto_needs_no_human_word(self) -> None:
        self.verify_state(autonomy="auto")
        pilot = self.pilot()
        pilot.tick()
        self.assertFalse((self.state_dir / "pilot" / "checkpoint-4.md").exists())

    def test_observe_writes_no_digest(self) -> None:
        self.verify_state(autonomy="observe")
        pilot = self.pilot()
        pilot.tick()
        self.assertFalse((self.state_dir / "pilot" / "checkpoint-4.md").exists())

    def test_unapplied_patch_is_named_in_the_digest(self) -> None:
        patch = self.state_dir / "patches" / "pair-codex-9.patch"
        patch.parent.mkdir(parents=True, exist_ok=True)
        patch.write_text("x", encoding="utf-8")
        self.verify_state(autonomy="semi",
                          patches={str(patch): {"owner": "codex", "sealed": True,
                                                "sealed_at": ago(9000), "applied": False}},
                          human_ok=[{"round": 4}])
        pilot = self.pilot()
        pilot.tick()
        text = (self.state_dir / "pilot" / "checkpoint-4.md").read_text(encoding="utf-8")
        self.assertIn("pair-codex-9.patch", text)

    def test_unknown_autonomy_is_treated_as_observe(self) -> None:
        """Состояние от более новой сборки не должно выдать этой сборке больше
        прав, чем человек просил."""
        self.verify_state(autonomy="turbo")
        pilot = self.pilot()
        pilot.tick()
        self.assertFalse((self.state_dir / "pilot" / "checkpoint-4.md").exists())


class TestReportNotCovered(PilotCase):
    def test_report_lists_unapplied_patches_and_failed_evidence(self) -> None:
        patch = self.state_dir / "patches" / "pair-codex-7.patch"
        patch.parent.mkdir(parents=True, exist_ok=True)
        patch.write_text("x", encoding="utf-8")
        self.write_state(phase="done", closed_at=ago(60), round=3, writer="agy", autonomy="auto",
                         roles={"agy": {"last_seen": ago(60), "pane": "%1"}},
                         evidence=[{"round": 3, "kind": "test", "value": "12 failed", "ok": False},
                                   {"round": 3, "kind": "lint", "value": "clean", "ok": True}],
                         patches={str(patch): {"owner": "codex", "sealed": True,
                                               "sealed_at": ago(9000), "applied": False}})
        pilot = self.pilot()
        pilot.tick()
        text = (self.state_dir / "REPORT.md").read_text(encoding="utf-8")
        self.assertIn("FAIL test", text)
        self.assertIn("pair-codex-7.patch", text)
        self.assertIn("НЕ ПОКРЫТО", text)

    def test_report_says_so_when_there_is_no_evidence(self) -> None:
        self.write_state(phase="done", closed_at=ago(60), round=3, writer="agy",
                         roles={"agy": {"last_seen": ago(60), "pane": "%1"}})
        self.pilot().tick()
        text = (self.state_dir / "REPORT.md").read_text(encoding="utf-8")
        self.assertIn("НИ ОДНОГО", text)


class TestOperatorSafety(PilotCase):
    """Сверхвизор не должен вредить тому, кто его запустил."""

    def test_own_pane_is_never_touched(self) -> None:
        """Панель, в которой живёт сам сверхвизор, — это панель человека. Вбить
        туда напоминание значит съесть то, что он печатает."""
        self.inbox("opencode", 4)
        self.write_state(writer="opencode", seen={"opencode": 0},
                         roles={"opencode": {"last_seen": ago(9999), "pane": "%19"}})
        pilot = self.pilot()
        pilot.own_pane = "%19"
        pilot.tick()
        self.assertEqual("", pilot.pending("opencode"), "в свою панель не кладём")
        self.assertIn("pilot_own_pane", self.transcript_kinds())

    def test_pane_of_another_role_is_still_fine(self) -> None:
        self.inbox("agy", 4)
        self.write_state(seen={"agy": 0}, roles={"agy": {"last_seen": ago(60), "pane": "%20"}})
        pilot = self.pilot()
        pilot.own_pane = "%19"
        pilot.tick()
        self.assertTrue(self.wake(pilot, "agy"), "чужая панель — наша работа")

    def test_daemon_ignores_inherited_tmux_pane(self) -> None:
        """Демон без PAIR_PILOT_PANE не усыновляет панель из окружения.

        Измерено 2026-10-07: пилот, запущенный из панели агента (%3332),
        наследовал TMUX_PANE и вечно пропускал роль этой панели через
        own_pane-skip — надзор за ней молча стоял. Демон стартует с чистым
        окружением, своя панель задаётся только явно через PAIR_PILOT_PANE.
        """
        os.environ["TMUX_PANE"] = "%20"
        os.environ.pop("PAIR_PILOT_PANE", None)
        try:
            pilot = self.pilot()
            self.assertIsNone(pilot.own_pane)
        finally:
            os.environ.pop("TMUX_PANE", None)

    def test_own_pane_recorded_as_a_session_address_is_still_own(self) -> None:
        """Панель человека может быть записана адресом сессии, а не `%N`.

        Сверхвизор знает свою панель из TMUX_PANE, то есть числом, а в ростере
        она лежит адресом `mail:0.0`. Сравнение строк на этом расходится, и
        сверхвизор начинает вбивать напоминания в панель оператора — то есть
        съедать его ввод. Это не гипотеза: адреса сессий уже пишутся в `_register`,
        а пилот читает то, что там написано.
        """
        self.inbox("opencode", 4)
        self.write_state(writer="opencode", seen={"opencode": 0},
                         roles={"opencode": {"last_seen": ago(9999), "pane": "mail:0.0"}})
        pilot = self.pilot()
        pilot.own_pane = "%19"
        # Настоящий ключ группировки вместо заглушки: здесь важно проверить
        # сравнение, а не то, что tmux выключен.
        pilot.pane_key = lambda pane: {
            "mail:0.0": "mail:0.0", "%19": "mail:0.0", "%20": "mail:0.1",
        }.get(pane, pane)
        pilot.tick()
        self.assertEqual("", pilot.pending("opencode"), "в свою панель не кладём")
        self.assertIn("pilot_own_pane", self.transcript_kinds())


class TestVisibleWork(PilotCase):
    """last_seen меряет участие в протоколе, а не работу."""

    def patch_composer(self, line, empty: bool) -> None:
        pairpilot.pane_alive = lambda tmux, pane: True
        pairpilot.composer_line = lambda lines: line
        pairpilot.is_composer_empty = lambda _line: empty

    def setUp(self) -> None:
        self._line = pairpilot.composer_line
        self._empty = pairpilot.is_composer_empty
        super().setUp()

    def tearDown(self) -> None:
        pairpilot.composer_line = self._line
        pairpilot.is_composer_empty = self._empty
        super().tearDown()

    def silent_writer_state(self) -> None:
        self.write_state(writer="agy", roles={"agy": {"last_seen": ago(9999), "pane": "%9"}})

    def test_busy_pane_is_not_nudged_even_when_the_clock_ran_out(self) -> None:
        self.silent_writer_state()
        pilot = self.pilot()
        pilot.tmux = "/usr/bin/true"
        self.patch_composer("> я дописываю тест", empty=False)
        pilot.tick()
        self.assertEqual("", pilot.pending("agy"),
                         "агент печатает — значит работает, и толкать его незачем")

    def test_idle_pane_is_woken_silently(self) -> None:
        """Пустой композер — дефект, и раньше он лечился печатью.

        Теперь он лечится файлом. Проверка переехала с «что в очереди печати» на
        «что в пробуждении», потому что по смыслу дефект один и тот же, а
        количество нажатий клавиш — как раз то, что мы убираем.
        """
        self.silent_writer_state()
        pilot = self.pilot()
        pilot.tmux = "/usr/bin/true"
        self.patch_composer(">", empty=True)
        pilot.tick()
        self.assertTrue(self.wake(pilot, "agy"), "пустой композер — вот это дефект")
        self.assert_enqueued_for_push(pilot, "agy")

    def test_missing_composer_counts_as_busy(self) -> None:
        """TUI перерисовывается, композера нет. Бить в этот момент — значит
        отправить клавиши в неизвестно что."""
        self.silent_writer_state()
        pilot = self.pilot()
        pilot.tmux = "/usr/bin/true"
        self.patch_composer(None, empty=True)
        pilot.tick()
        self.assertEqual("", pilot.pending("agy"))


class TestObligations(PilotCase):
    """Запрос, на который не ответили, обязан быть виден. Иначе он тихо умирает
    в ящике, а пара выглядит спокойно."""

    def obligation(self, seq: int, sender: str, receiver: str, age: int) -> None:
        root = self.state_dir / "obligations"
        root.mkdir(parents=True, exist_ok=True)
        (root / f"{seq:06d}-x.json").write_text(json.dumps({
            "seq": seq, "from": sender, "to": receiver,
            "opened_at": ago(age), "subject": f"вопрос {seq}",
        }), encoding="utf-8")

    def test_fresh_obligation_is_left_alone(self) -> None:
        self.obligation(1, "opencode", "agy", age=60)
        self.write_state(roles={"agy": {"last_seen": ago(30), "pane": "%20"}})
        pilot = self.pilot()
        pilot.tick()
        self.assertEqual("", pilot.pending("agy"), "только что спросили — ждём")

    def test_recipient_poked_within_five_minutes(self) -> None:
        """P1: затык будится за <5 мин. Граница 180с: 60с — молчим, 200с — будим."""
        self.obligation(8, "opencode", "agy", age=60)
        self.write_state(roles={"agy": {"last_seen": ago(30), "pane": "%20"}})
        pilot = self.pilot()
        pilot.tick()
        self.assertEqual("", pilot.pending("agy"), "минута — ещё ждём")
        self.obligation(9, "opencode", "agy", age=200)
        pilot.tick()
        self.assertIn("ответа в его почте нет", pilot.pending("agy"))

    def test_recipient_is_poked_first(self) -> None:
        self.obligation(7, "opencode", "agy", age=1000)
        self.write_state(roles={"agy": {"last_seen": ago(30), "pane": "%20"}})
        pilot = self.pilot()
        pilot.tick()
        self.assertIn("ответа в его почте нет", pilot.pending("agy"))
        self.assertEqual("", pilot.pending("opencode"), "автор пока не в курсе, рано")

    def test_author_is_told_when_the_wait_stretches(self) -> None:
        self.obligation(7, "opencode", "agy", age=3000)
        self.write_state(roles={"agy": {"last_seen": ago(30), "pane": "%20"}})
        pilot = self.pilot()
        pilot.tick()
        self.assertIn("ответа в его почте нет", pilot.pending("agy"))
        self.assertIn("жди вечно", pilot.pending("opencode"))

    def test_answered_obligation_stops_being_nagged(self) -> None:
        self.obligation(7, "opencode", "agy", age=3000)
        (self.state_dir / "obligations" / "000007-x.json").unlink()
        self.write_state(roles={"agy": {"last_seen": ago(30), "pane": "%20"}})
        pilot = self.pilot()
        pilot.tick()
        self.assertEqual("", pilot.pending("agy"))
        self.assertEqual("", pilot.pending("opencode"))

    def test_broken_obligation_file_is_cleaned_not_crashed_on(self) -> None:
        root = self.state_dir / "obligations"
        root.mkdir(parents=True, exist_ok=True)
        (root / "000001-bad.json").write_text("{не json", encoding="utf-8")
        self.write_state(roles={"agy": {"last_seen": ago(30), "pane": "%20"}})
        pilot = self.pilot()
        pilot.tick()
        self.assertEqual([], list(root.glob("*.json")), "битое обязательство убираем")

    def test_skipped_role_gets_no_obligation_nag(self) -> None:
        self.obligation(7, "codex", "agy", age=3000)
        self.write_state(roles={"agy": {"last_seen": ago(30), "pane": "%20"},
                                "codex": {"last_seen": ago(30), "pane": "%20"}})
        pilot = self.pilot()
        pilot.tick()
        self.assertEqual("", pilot.pending("agy"), "общая панель — не тыкаем")


if __name__ == "__main__":
    unittest.main(verbosity=2)
