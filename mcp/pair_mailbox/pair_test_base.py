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
        # Фикс раунда 48 (условие A вердикта opencode): pair_send вне ростера
        # отказывается, поэтому peer-роль обязана зарегистрироваться до писем.
        try:
            self.peer.tool_pair_state()
        except Exception:  # noqa: BLE001 - регистрация не должна ронять setUp
            pass

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
        for phase in ("plan", "implement", "verify"):
            self.writer.tool_pair_advance(phase)
        self.record_test_evidence(value)
        self.human_ok()
        return self.writer.tool_pair_advance(phase="done")

    def set_autonomy(self, level: str) -> None:
        """Объявить уровень явно.

        Жил этот помощник внутри одного класса, и шесть тестов в других полагались на
        дефолт молча — то есть контракт уровней не был закреплён ни одним из них, и
        смена дефолта ломала тесты, к дефолту отношения не имеющие.
        """
        self.store.mutate(lambda state: state.__setitem__("autonomy", level))