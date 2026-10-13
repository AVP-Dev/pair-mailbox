#!/usr/bin/env python3
"""Событийное пробуждение без композера (wake-without-composer).

Проверяет контракт: письмо/заметка всегда оставляют файл wake/<role>.json
(гарантия для опроса pairpilot), а _ring — только оптимизация без ожидания:
занятый/невидимый композер возвращается сразу, инструмент не висит.
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pair_test_base import PairTestCase  # noqa: E402


class TestEventWake(PairTestCase):
    def wake_file(self, role: str) -> Path:
        return self.root / "wake" / f"{role}.json"

    def test_send_writes_wake_even_when_ring_disabled(self) -> None:
        """Письмо оставляет файл пробуждения, даже если звонок отключён.

        Иначе «файловый канал как гарантия» — слова: при PAIR_DISABLE_RING=1
        (как в наборах) сосед не узнал бы ничего.
        """
        os.environ["PAIR_DISABLE_RING"] = "1"
        self.writer.tool_pair_state()
        self.peer.tool_pair_state()
        self.writer.tool_pair_send(to="reviewer-agent", text="возьми задачу")
        path = self.wake_file("reviewer-agent")
        self.assertTrue(path.exists(), "send обязан записать wake-файл по событию")
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual("reviewer-agent", payload.get("role"))
        self.assertIn("message", payload.get("reasons", []))

    def test_note_writes_wake_for_peer(self) -> None:
        os.environ["PAIR_DISABLE_RING"] = "1"
        self.writer.tool_pair_state()
        self.peer.tool_pair_state()
        self.writer.tool_pair_note(kind="plan", text="план готов")
        path = self.wake_file("reviewer-agent")
        self.assertTrue(path.exists(), "note обязана разбудить соседа файлом")
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertTrue(any("заметка/plan" in str(r) for r in payload.get("reasons", [])))

    def test_note_does_not_ring_peers(self) -> None:
        """Заметка — тихая: wake-файл да, слепой звонок в композер нет.

        tool_pair_note раньше веером звонил всем ролям на каждой заметке;
        при 30+ заметках за вечер это N слепых send-keys Enter в чужие
        композеры. Пробуждение — через wake-файл, который опрашивает пилот.
        """
        self.writer.tool_pair_state()
        self.peer.tool_pair_state()
        with mock.patch.object(self.writer, "_ring",
                               wraps=self.writer._ring) as ring:
            result = self.writer.tool_pair_note(kind="plan", text="план готов")
        rung = [c for c in ring.call_args_list]
        self.assertEqual([], rung, "note не должна звонить никому")
        self.assertEqual({}, result.get("woke_peers"),
                         "woke_peers остаётся пустым ради совместимости")
        kinds = [e.get("kind") for e in self.store.read_transcript(limit=0)]
        self.assertNotIn("doorbell", kinds, "в транскрипте нет звонков")
        path = self.wake_file("reviewer-agent")
        self.assertTrue(path.exists(), "wake-файл по-прежнему пишется")

    def test_wake_merge_keeps_asked_at(self) -> None:
        """Висящее пробуждение дополняется, а не перезаписывается.

        Иначе два повода (непрочитанное + молчание) эскалировались бы дважды —
        та же ошибка, что чинилась в раунде 15 для пилота.
        """
        from wake import record_wake
        os.environ["PAIR_DISABLE_RING"] = "1"
        self.writer.tool_pair_state()
        self.peer.tool_pair_state()
        state = self.store.read()
        record_wake(self.root, state, "reviewer-agent", "первый", "текст 1")
        first = json.loads(self.wake_file("reviewer-agent").read_text(encoding="utf-8"))
        record_wake(self.root, self.store.read(), "reviewer-agent", "второй", "текст 2")
        second = json.loads(self.wake_file("reviewer-agent").read_text(encoding="utf-8"))
        self.assertEqual(first.get("asked_at"), second.get("asked_at"),
                         "asked_at обязан сохраняться, иначе терпение сбрасывается")
        self.assertIn("первый", second.get("reasons", []))
        self.assertIn("второй", second.get("reasons", []))

    def test_ring_returns_immediately_when_composer_busy(self) -> None:
        """Занятый композер — сразу в файл, без 1.5с ожидания и перехвата ввода.

        Мутация: вернуть цикл deadline+sleep — тест обязан покраснеть
        (sleep будет вызван).
        """
        import composer
        import server as server_mod
        self.store.mutate(lambda s: s.setdefault("roles", {})
                          .setdefault("reviewer-agent", {}).update({"pane": "%45"}))
        os.environ.pop("PAIR_DISABLE_RING", None)
        orig_line = composer.composer_line
        orig_which = composer.shutil.which
        orig_foreign = composer.pane_is_foreign
        composer.composer_line = lambda lines: "> печатаю"
        composer.shutil.which = lambda name: "/usr/bin/tmux"
        composer.pane_is_foreign = lambda tmux, pane, root: (False, "")
        sleeps = []
        orig_sleep = server_mod.time.sleep
        server_mod.time.sleep = lambda s: sleeps.append(s)  # type: ignore[method-assign]
        orig_sub_run = server_mod.subprocess.run

        def fake_sub_run(cmd, *a, **kw):
            if isinstance(cmd, (list, tuple)) and len(cmd) >= 2 and cmd[1] == "display-message":
                return server_mod.subprocess.CompletedProcess(cmd, 0, stdout="0\n", stderr="")
            return orig_sub_run(cmd, *a, **kw)

        server_mod.subprocess.run = fake_sub_run  # type: ignore[method-assign]
        try:
            result = self.writer._ring("reviewer-agent", "message")
        finally:
            composer.composer_line = orig_line
            composer.shutil.which = orig_which
            composer.pane_is_foreign = orig_foreign
            server_mod.time.sleep = orig_sleep  # type: ignore[method-assign]
            server_mod.subprocess.run = orig_sub_run  # type: ignore[method-assign]
        self.assertFalse(result["rung"], "в занятый композер не лезем")
        self.assertIn("занят", result["why"])
        self.assertEqual([], sleeps, "ожидание композера обязано быть single-shot без sleep")


if __name__ == "__main__":
    unittest.main(verbosity=2)
