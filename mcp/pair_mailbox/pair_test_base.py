"""Общая база для наборов тестов pair-mailbox.

Это НЕ тест-файл, а приспособление: его нет соответствия исходнику, и называть его
третьим тест-файлом было бы неверно. Живёт отдельно от `test_protocol.py` потому,
что после разбора `server.py` наборов стало несколько, а база у них одна: поднятое
состояние пары в временном каталоге, две роли поверх него и несколько помощников,
которые повторялись бы в каждом файле построчно.

Правило «один тест-файл на один исходник» здесь не нарушено: база не проверяет
поведение, она только готовит почву. Проверки живут в файлах, названных по
исходникам, которые они проверяют.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from server import PairSession, ProtocolError, Store

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SERVER = Path(__file__).resolve().parent / "server.py"
PAIRCHAT = REPO_ROOT / "bin" / "pairchat"


class PairTestCase(unittest.TestCase):
    def allow_panes(self) -> None:
        """Считать любую панель своей, на время теста.

        Нужно тестам, которые проверяют доставку, а не адресацию: там
        `_register` не должен отвергать панель по недоказанности, иначе тест
        падает раньше, чем дойдёт до своего предмета.

        Подменяются оба вопроса — «своя ли панель» и «не чужой ли проект»,
        потому что `_register` спрашивает второй, а `pair_deliver` первый.
        Подменить один значит проверять не ту ветку: так и случилось, когда
        появилась вторая проверка, а подмена осталась только на первой.
        """
        import composer

        real_foreign = composer.pane_is_foreign
        real_belongs = composer.pane_belongs_to_project
        composer.pane_is_foreign = lambda tmux, pane, root: (False, "")
        composer.pane_belongs_to_project = lambda tmux, pane, root: (True, "")
        self.addCleanup(setattr, composer, "pane_is_foreign", real_foreign)
        self.addCleanup(setattr, composer, "pane_belongs_to_project", real_belongs)

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "state"
        self.store = Store(self.root)
        self.writer = PairSession("writer-agent", self.store)
        self.peer = PairSession("reviewer-agent", self.store)
        self._orig_tmux_pane = os.environ.pop("TMUX_PANE", None)
        # Запоминаем ДО того, как среда очищена: тесты ниже проверяют pane_exists
        # против настоящей панели, а setUp убирает переменную, чтобы роли в наборе
        # не слали спам в окно оператора (F-08). Без этого запоминания такая
        # проверка пропускалась бы всегда, то есть никогда.
        self._pane_at_startup = self._orig_tmux_pane
        self._orig_disable_ring = os.environ.get("PAIR_DISABLE_RING")
        os.environ["PAIR_DISABLE_RING"] = "1"
        # Кэш состояния в tool_pair_state возвращает заглушку без obligations и
        # evidence_this_round на второй подряд вызов. Тесты проверяют контракт
        # «каждый зов возвращает полное состояние», поэтому кэш здесь выключен
        # (сервер читает PAIR_DISABLE_STATE_CACHE — см. tool_pair_state).
        self._orig_disable_state_cache = os.environ.get("PAIR_DISABLE_STATE_CACHE")
        os.environ["PAIR_DISABLE_STATE_CACHE"] = "1"
        # Адресация по ancestry выключена ДО регистрации: лишние записи self-address
        # не должны попадать в fill (287ff1d).
        os.environ["PAIR_DISABLE_ANCESTRY_PANE"] = "1"
        # Регистрация ДО писем идёт mutate-merge, а не tool_pair_state: последний
        # перезаписывает запись целиком и сносит поля, выставленные тестом позже.
        self.store.mutate(lambda s: s.setdefault("roles", {}).setdefault(
            "reviewer-agent", {}))
        # Дефолтный intent для существующих наборов тестов
        self.write_intent(1)

    def tearDown(self) -> None:
        self._tmp.cleanup()
        if self._orig_tmux_pane is not None:
            os.environ["TMUX_PANE"] = self._orig_tmux_pane
        else:
            os.environ.pop("TMUX_PANE", None)
        if self._orig_disable_ring is not None:
            os.environ["PAIR_DISABLE_RING"] = self._orig_disable_ring
        else:
            os.environ.pop("PAIR_DISABLE_RING", None)
        if self._orig_disable_state_cache is not None:
            os.environ["PAIR_DISABLE_STATE_CACHE"] = self._orig_disable_state_cache
        else:
            os.environ.pop("PAIR_DISABLE_STATE_CACHE", None)
        os.environ.pop("PAIR_DISABLE_ANCESTRY_PANE", None)

    def assertRefused(self, callable_, *args, contains: str = "", **kwargs):
        with self.assertRaises(ProtocolError) as caught:
            callable_(*args, **kwargs)
        if contains:
            self.assertIn(contains, str(caught.exception).lower())
        return caught.exception

    def record_test_evidence(self, value: str = "117 passed in 6.2s (exit 0)", ok: bool = True,
                             task: int | None = None) -> None:
        """Record what a closing round needs: a machine-produced fact for THIS round."""
        self.writer.tool_pair_evidence(kind="test", value=value, ok=ok, task=task)

    def human_ok(self) -> None:
        """The one word a human says at a semi checkpoint, without leaving Python."""
        self.store.mutate(lambda state: state.setdefault("human_ok", []).append(
            {"round": int(state.get("round", 0)), "at": "now", "phase": state.get("phase")}))

    def close_round(self, value: str = "117 passed in 6.2s (exit 0)") -> dict:
        """Walk the phases and close, satisfying the gate the way a real pair would."""
        self.advance_phases()
        self.record_test_evidence(value)
        self.human_ok()
        return self.writer.tool_pair_advance(phase="done")

    def write_intent(self, round_number: int) -> None:
        """Артефакт намерения для гейта scoping → plan (раунд 57)."""
        import intent_gate
        body = [f"round: {round_number}", "",
                "## Problem", "тестовая проблема", "",
                "## Scope", "границы теста", "",
                "## Outcome", "ожидаемый результат теста", ""]
        (self.store.root / intent_gate.INTENT_FILE).write_text("\n".join(body), encoding="utf-8")

    def write_plan(self, round_number: int) -> None:
        """Артефакт плана для машинного гейта plan → implement (манифест 53).

        Фикстура пишет план ТЕКУЩЕГО раунда: гейт сверяет маркер `round: N`
        с номером раунда, и план прошлого раунда был бы отказом — ровно та
        защита от устаревшего артефакта, которую гейт и добавляет.
        """
        import plan_gate
        body = [f"round: {round_number}", "",
                "## Goal", "тест", "",
                "## Invariants", "тест", "",
                "## Acceptance", "тест", ""]
        (self.store.root / plan_gate.PLAN_FILE).write_text("\n".join(body), encoding="utf-8")

    def ack_plan(self, round_number: int) -> None:
        """Виза критика: заметка verdict со строкой plan-ack round N.

        mtime заметки двигается вперёд относительно плана: гейт требует, чтобы
        виза была не раньше плана (заметка из прошлого раунда не считается).
        """
        import os
        import time
        from datetime import datetime, timezone
        stamp = time.time() + 1
        note = self.store.root / "notes" / "verdict.md"
        note.parent.mkdir(parents=True, exist_ok=True)
        header = ("## " + datetime.fromtimestamp(stamp, tz=timezone.utc).isoformat()
                  + " — reviewer-agent\n")
        note.write_text(header + f"\nplan-ack round {round_number}\n", encoding="utf-8")
        os.utime(note, (stamp, stamp))

    def advance_phases(self, phases: tuple = ("plan", "implement", "verify")) -> None:
        """Пройти фазы так, как их проходит настоящая пара: с планом и визой.

        До гейта план→implement проход был свободным, и фикстуры просто шли по
        фазам. Теперь переход требует артефакта — помощник пишет его от имени
        пары, чтобы тесты проверяли свой предмет, а не гейт.
        """
        for phase in phases:
            round_number = int(self.store.read().get("round", 0))
            if phase == "plan":
                self.write_intent(round_number)
            elif phase == "implement":
                self.write_plan(round_number)
                self.ack_plan(round_number)
            self.writer.tool_pair_advance(phase)

    def set_autonomy(self, level: str) -> None:
        """Объявить уровень явно.

        Жил этот помощник внутри одного класса, и шесть тестов в других полагались на
        дефолт молча — то есть контракт уровней не был закреплён ни одним из них, и
        смена дефолта ломала тесты, к дефолту отношения не имеющие.
        """
        self.store.mutate(lambda state: state.__setitem__("autonomy", level))