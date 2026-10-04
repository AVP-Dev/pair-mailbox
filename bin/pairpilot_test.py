#!/usr/bin/env python3
"""Тесты сверхвизора: проверяют что он ДЕЛАЕТ, а не что файл существует.

Каждый тест поднимает своё состояние в tmp и смотрит на результат: появилось ли
отложенное сообщение, записано ли событие в транскрипт, написан ли отчёт.

Запуск:  python3 bin/pairpilot_test.py
"""
import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "mcp" / "pair_mailbox"))

_spec = importlib.util.spec_from_loader("pairpilot", None)
pairpilot = importlib.util.module_from_spec(_spec)
pairpilot.__dict__["__file__"] = str(ROOT / "bin" / "pairpilot")
exec(compile((ROOT / "bin" / "pairpilot").read_text(encoding="utf-8"), "pairpilot", "exec"), pairpilot.__dict__)


def ago(seconds: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


class PilotCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.tmp.name) / ".pair"
        self.state_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def pilot(self, silence: float = 600.0) -> "pairpilot.Pilot":
        # tmux выключаем: тесты не должны дёргать живые окна.
        p = pairpilot.Pilot(self.state_dir, silence=silence, interval=1)
        p.tmux = None
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
        }
        state.update(fields)
        (self.state_dir / "state.json").write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        return state

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


class TestLiveness(PilotCase):
    def test_silent_writer_is_nudged_once_only(self) -> None:
        self.write_state(writer="agy", roles={"agy": {"last_seen": ago(3600), "pid": 1, "pane": "%1"}})
        pilot = self.pilot()
        pilot.tick()
        first = pilot.pending("agy")
        self.assertTrue(first, "молчащий писатель должен получить напоминание")
        self.assertIn("право записи", first)
        pilot.tick()
        self.assertEqual(first, pilot.pending("agy"), "повторно в очередь не кладём")

    def test_fresh_writer_is_left_alone(self) -> None:
        self.write_state(writer="agy", roles={"agy": {"last_seen": ago(30), "pane": "%1"}})
        pilot = self.pilot()
        pilot.tick()
        self.assertEqual("", pilot.pending("agy"), "молчание в пределах нормы не трогаем")

    def test_nudge_is_not_queued_when_round_is_done(self) -> None:
        self.write_state(writer="agy", phase="done", closed_at=ago(10),
                         roles={"agy": {"last_seen": ago(3600), "pane": "%1"}})
        pilot = self.pilot()
        pilot.tick()
        self.assertEqual("", pilot.pending("agy"), "закрытый раунд не будим")

    def test_critic_silence_is_not_a_nudge_by_itself(self) -> None:
        """Молчание критика — не дефект: возможно, у него нет работы. Толкать
        нечего, и бить его значит мешать."""
        self.write_state(writer="agy", roles={"agy": {"last_seen": ago(30), "pane": "%1"},
                                              "codex": {"last_seen": ago(3600), "pane": "%2"}})
        pilot = self.pilot()
        pilot.tick()
        self.assertEqual("", pilot.pending("codex"))


class TestUnread(PilotCase):
    def test_unread_mail_produces_a_reminder(self) -> None:
        self.inbox("agy", 3)
        self.write_state(roles={"agy": {"last_seen": ago(60), "pane": "%1"}}, seen={"agy": 0})
        pilot = self.pilot()
        pilot.tick()
        self.assertIn("непрочитанных", pilot.pending("agy"))

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

    def test_short_silence_threshold_nudges(self) -> None:
        self.write_state(writer="agy", roles={"agy": {"last_seen": ago(400), "pane": "%1"}})
        strict = self.pilot(silence=100)
        strict.tick()
        self.assertTrue(strict.pending("agy"), "400с молчания при пороге 100с — дефект")


class TestTmuxPaths(PilotCase):
    """Пути, которые зовут tmux. Им нужна подмена, иначе они проверяются
    только в бою — а именно там падал append_transcript с чужим role=."""

    def fake_tmux(self, alive=True, prompt=None):
        pilot = self.pilot()
        pilot.tmux = "/usr/bin/true"
        pairpilot.pane_alive = lambda tmux, pane: alive
        pairpilot.resolve_permission_prompt = lambda tmux, pane: prompt
        return pilot

    def tearDown(self) -> None:
        pairpilot.pane_alive = self._real_alive
        pairpilot.resolve_permission_prompt = self._real_prompt
        super().tearDown()

    def setUp(self) -> None:
        self._real_alive = getattr(pairpilot, "pane_alive", None)
        self._real_prompt = getattr(pairpilot, "resolve_permission_prompt", None)
        super().setUp()

    def test_missing_pane_is_recorded_without_crashing(self) -> None:
        self.write_state(roles={"codex": {"last_seen": ago(10)}})
        pilot = self.fake_tmux()
        pilot.tick()
        self.assertIn("pilot_pane_missing", self.transcript_kinds())

    def test_dead_pane_is_recorded_once(self) -> None:
        self.write_state(roles={"agy": {"last_seen": ago(10), "pane": "%9"}})
        pilot = self.fake_tmux(alive=False)
        pilot.tick()
        pilot.tick()
        self.assertEqual(1, self.transcript_kinds().count("pilot_pane_dead"),
                         "о мёртвой панели говорим один раз, а не каждые 15 секунд")

    def test_permission_prompt_is_confirmed_and_logged(self) -> None:
        self.write_state(roles={"agy": {"last_seen": ago(10), "pane": "%9"}})
        pilot = self.fake_tmux(prompt="Allow execution of bash?")
        pilot.tick()
        self.assertIn("pilot_unblock", self.transcript_kinds())

    def test_live_pane_produces_no_panic(self) -> None:
        self.write_state(writer="agy", roles={"agy": {"last_seen": ago(10), "pane": "%9"}})
        pilot = self.fake_tmux()
        pilot.tick()
        self.assertNotIn("pilot_pane_dead", self.transcript_kinds())


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
        self.assertTrue(pilot.pending("agy"), "чужая панель — наша работа")


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

    def test_idle_pane_is_nudged(self) -> None:
        self.silent_writer_state()
        pilot = self.pilot()
        pilot.tmux = "/usr/bin/true"
        self.patch_composer(">", empty=True)
        pilot.tick()
        self.assertTrue(pilot.pending("agy"), "пустой композер — вот это дефект")

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
