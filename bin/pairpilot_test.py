#!/usr/bin/env python3
"""Тесты сверхвизора: проверяют что он ДЕЛАЕТ, а не что файл существует.

Каждый тест поднимает своё состояние в tmp и смотрит на результат: появилось ли
отложенное сообщение, записано ли событие в транскрипт, написан ли отчёт.

Запуск:  python3 bin/pairpilot_test.py
"""
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
            # Явный пустой список: состояние говорит на новом языке, и старая
            # строка task не поднимается обратно открытой задачей. Иначе каждый
            # тест этого файла получал бы блокер «задача не сделана».
            "tasks": [],
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

    def test_dead_pane_is_recorded_once(self) -> None:
        self.write_state(roles={"agy": {"last_seen": ago(10), "pane": "%9"}})
        pilot = self.fake_tmux(alive=False)
        pilot.tick()
        pilot.tick()
        self.assertEqual(1, self.transcript_kinds().count("pilot_pane_dead"),
                         "о мёртвой панели говорим один раз, а не каждые 15 секунд")

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
        super().setUp()

    def tearDown(self) -> None:
        pairpilot.subprocess = self._real_subprocess
        super().tearDown()

    def dead_writer_state(self, **extra) -> None:
        self.write_state(writer="agy", round=3,
                         roles={"agy": {"last_seen": ago(600), "pid": self.DEAD_PID, "pane": "%7"}},
                         **extra)

    def pilot_with_launch(self, command: str = "agy --resume"):
        pilot = self.pilot()
        pilot.tmux = "/usr/bin/tmux"
        pairpilot.pane_alive = lambda tmux, pane: True
        pilot.launch = {"agy": command} if command else {}
        return pilot

    def test_dead_writer_is_respawned(self) -> None:
        self.dead_writer_state()
        pilot = self.pilot_with_launch()
        pilot.tick()
        self.assertEqual(["/usr/bin/tmux", "respawn-pane", "-k", "-t", "%7", "agy --resume"],
                         self.calls[0])
        self.assertIn("pilot_restart", self.transcript_kinds())

    def test_restart_happens_once_per_dead_pid(self) -> None:
        self.dead_writer_state()
        pilot = self.pilot_with_launch()
        pilot.tick()
        pilot.tick()
        self.assertEqual(1, self.calls.count(
            ["/usr/bin/tmux", "respawn-pane", "-k", "-t", "%7", "agy --resume"]),
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
        respawns = [call for call in self.calls if "respawn-pane" in call]
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
        self.assertEqual(pilot.restart_limit, len([c for c in self.calls if "respawn-pane" in c]))
        # Окно истекло: попытки вне окна не должны считаться.
        pilot.restarts_file("agy").write_text(
            " ".join(str(int(time.time() - 3600)) for _ in range(pilot.restart_limit)),
            encoding="utf-8")
        self.write_state(writer="agy", round=3,
                         roles={"agy": {"last_seen": ago(600), "pid": 998111, "pane": "%7"}})
        pilot.tick()
        self.assertEqual(pilot.restart_limit + 1,
                         len([c for c in self.calls if "respawn-pane" in c]))

    def test_budget_is_per_role_not_global(self) -> None:
        """Исчерпание попыток у agy не должно запрещать перезапуск claude."""
        pilot = self.pilot_with_launch()
        pilot.launch["claude"] = "claude"
        for _ in range(pilot.restart_limit + 2):
            self.write_state(writer="agy", round=3,
                             roles={"agy": {"last_seen": ago(600), "pid": self.DEAD_PID,
                                            "pane": "%7"}})
            pilot.tick()
        self.write_state(writer="agy", round=3,
                         roles={"agy": {"last_seen": ago(600), "pid": self.DEAD_PID, "pane": "%7"},
                                "claude": {"last_seen": ago(600), "pid": self.DEAD_PID, "pane": "%8"}})
        pilot.tick()
        self.assertTrue(any("claude" in str(call) for call in self.calls),
                        "у другого агента свой бюджет")

    def test_live_process_is_never_respawned(self) -> None:
        """Молчание лечится напоминанием, смерть — перезапуском. Убить того,
        кто просто занят работой, — значит отнять у него контекст."""
        self.write_state(writer="agy",
                         roles={"agy": {"last_seen": ago(10), "pid": os.getpid(), "pane": "%7"}})
        pilot = self.pilot_with_launch()
        pilot.tick()
        self.assertEqual([], self.calls)
        self.assertNotIn("pilot_restart", self.transcript_kinds())

    def test_restart_needs_a_known_command(self) -> None:
        """Нет команды — нет перезапуска и громкая запись об этом. Выдуманная
        команда хуже отсутствующей: панель оживает и сразу падает."""
        self.dead_writer_state()
        pilot = self.pilot_with_launch(command="")
        pilot.tick()
        self.assertEqual([], self.calls)
        self.assertIn("pilot_restart_impossible", self.transcript_kinds())

    def test_dead_process_without_a_pane_is_left_to_the_human(self) -> None:
        """Окно, закрытое человеком, не поднимается молча: это не зависание,
        это решение."""
        self.write_state(writer="agy",
                         roles={"agy": {"last_seen": ago(600), "pid": self.DEAD_PID, "pane": "%7"}})
        pilot = self.pilot_with_launch()
        pairpilot.pane_alive = lambda tmux, pane: False
        pilot.tick()
        self.assertEqual([], self.calls)
        self.assertIn("pilot_restart_impossible", self.transcript_kinds())

    def test_observe_mode_does_not_restart(self) -> None:
        self.dead_writer_state(autonomy="observe")
        pilot = self.pilot_with_launch()
        pilot.tick()
        self.assertEqual([], self.calls, "в observe перезапуск — это действие, а не сообщение")

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
        pilot.own_pane = "%7"
        pilot.tick()
        self.assertEqual([], self.calls, "свою панель не перезапускаем никогда")
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
        self.assertEqual([], self.calls, "общую панель не перезапускаем")
        self.assertNotIn("pilot_restart", self.transcript_kinds())
        self.assertIn("pilot_pane_shared", self.transcript_kinds())

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
