#!/usr/bin/env python3
"""Тесты команд человека, меняющих раунд: `pairctl assign/roles/mode/autonomy/task`.

Соответствуют `pairctl_authority.py` — по одному тест-файлу на исходник. Общая
база живёт в `pair_test_base.py`, это приспособление, а не третий тест-файл.

Запуск:  python3 mcp/pair_mailbox/test_pairctl_authority.py
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pair_test_base import SERVER, PairTestCase  # noqa: E402
from pairctl_read import project_row, render_table  # noqa: E402
from server import Store, main  # noqa: E402

class TestGlobalRoles(PairTestCase):
    """Расстановка, заданная разом для всех проектов, должна реально действовать,
    а не просто лежать в файле."""

    def setUp(self) -> None:
        super().setUp()
        self._orig_roles = os.environ.get("PAIR_ROLES")
        self._roles_file = Path(self._tmp.name) / "roles.json"
        os.environ["PAIR_ROLES"] = str(self._roles_file)

    def tearDown(self) -> None:
        if self._orig_roles is not None:
            os.environ["PAIR_ROLES"] = self._orig_roles
        else:
            os.environ.pop("PAIR_ROLES", None)
        super().tearDown()

    def test_global_policy_applies_to_a_project_without_one(self) -> None:
        self._roles_file.write_text(
            json.dumps({"writer": "writer-agent", "critic": "reviewer-agent"}), encoding="utf-8")
        self.writer.tool_pair_state()  # обе роли должны появиться в ростере
        self.peer.tool_pair_state()
        self.assertEqual(self.peer.tool_pair_state()["my_standing"],
                         "критик по постоянной расстановке")

    def test_global_policy_is_ignored_when_its_roles_never_show_up(self) -> None:
        """Ролизация «opencode пишет» в проекте, где opencode не запускают, не
        должна оставлять проект без писателя: это тихая остановка работы."""
        self._roles_file.write_text(
            json.dumps({"writer": "opencode", "critic": "agy"}), encoding="utf-8")
        self.writer.tool_pair_state()
        self.peer.tool_pair_state()
        self.assertIn("гонк", self.writer.tool_pair_state()["my_standing"])
        self.assertNotIn("policy", self.store.read())

    def test_project_policy_wins_over_the_global_one(self) -> None:
        self._roles_file.write_text(
            json.dumps({"writer": "writer-agent", "critic": "reviewer-agent"}), encoding="utf-8")
        self.writer.tool_pair_state()
        self.peer.tool_pair_state()
        self.store.mutate(lambda st: st.__setitem__("policy", {"writer": "reviewer-agent"}))
        self.assertEqual(self.peer.tool_pair_state()["my_standing"],
                         "writer по постоянной расстановке")

    def test_missing_or_broken_global_file_leaves_the_race(self) -> None:
        self.assertIn("гонк", self.writer.tool_pair_state()["my_standing"])
        self._roles_file.write_text("{не json", encoding="utf-8")
        self.assertIn("гонк", self.writer.tool_pair_state()["my_standing"])

    def test_global_file_without_a_writer_is_ignored(self) -> None:
        self._roles_file.write_text(json.dumps({"critic": "reviewer-agent"}), encoding="utf-8")
        self.writer.tool_pair_state()
        self.peer.tool_pair_state()
        self.assertIn("гонк", self.writer.tool_pair_state()["my_standing"])



class TestAutonomyCommand(PairTestCase):
    """pairctl autonomy и pairctl ok: две команды человека, которыми он рулит."""

    def ctl(self, *argv: str) -> int:
        return main(["--state", str(self.root), *argv])

    def test_autonomy_is_set_and_logged(self) -> None:
        self.assertEqual(self.ctl("autonomy", "auto"), 0)
        self.assertEqual(self.store.read()["autonomy"], "auto")
        kinds = [entry["kind"] for entry in self.store.read_transcript()]
        self.assertIn("autonomy", kinds)

    def test_unknown_autonomy_is_refused(self) -> None:
        # argparse печатает usage и выходит с 2 — так же, как у `pairctl mode`.
        with self.assertRaises(SystemExit) as caught:
            self.ctl("autonomy", "ludicrous")
        self.assertEqual(caught.exception.code, 2)

    def test_ok_is_one_word_and_repeatable(self) -> None:
        self.writer.tool_pair_claim(reason="writer")
        self.assertEqual(self.ctl("ok"), 0)
        entries = self.store.read()["human_ok"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(self.ctl("ok"), 0)
        self.assertEqual(len(self.store.read()["human_ok"]), 1, "повторное «да» не плодит записи")

    def test_ok_is_scoped_to_the_round(self) -> None:
        self.writer.tool_pair_claim(reason="writer")
        self.ctl("ok")
        self.store.mutate(lambda state: state.__setitem__("round", 99))
        self.assertFalse(self.writer.human_ok_for_round(self.store.read()))



class TestControlSurface(PairTestCase):
    """pairctl is the human's side of the authority boundary."""

    def _ctl(self, *args: str) -> int:
        return main(["--state", str(self.root), *args])

    def test_human_assigns_and_releases(self) -> None:
        self.writer.tool_pair_state()
        self.peer.tool_pair_state()

        self._ctl("assign", "reviewer-agent")
        self.assertEqual(self.store.read()["writer"], "reviewer-agent")

        # authority moved: the new holder edits directly, the old writer proposes
        self.assertRefused(self.peer.tool_pair_patch_reserve, contains="write authority")
        self.assertIn("path", self.writer.tool_pair_patch_reserve())

        self._ctl("release")
        self.assertIsNone(self.store.read()["writer"])

    def test_human_can_assign_over_a_live_writer(self) -> None:
        self.writer.tool_pair_state()
        self.peer.tool_pair_state()
        self.writer.tool_pair_claim()
        self._ctl("assign", "reviewer-agent")
        self.assertEqual(self.store.read()["writer"], "reviewer-agent")

    def test_task_and_reset(self) -> None:
        self._ctl("task", "fix the retry storm")
        self.assertEqual(self.store.read()["task"], "fix the retry storm")

        self.assertEqual(self._ctl("reset"), 2, "reset must refuse without --yes")
        self.assertEqual(self.store.read()["task"], "fix the retry storm")

        self.assertEqual(self._ctl("reset", "--yes"), 0)
        self.assertEqual(self.store.read()["task"], "")

    def test_reset_archives_the_transcript_and_warns_about_fix(self) -> None:
        """reset стирал append-only транскрипт (§11) и молча возвращал fix.
        Теперь архив transcript-<дата>.jsonl, новый журнал начинается с reset,
        а вывод предупреждает о выключенном audit-гарде."""
        self._ctl("task", "something to lose")
        before = len(self.store.read_transcript(1000))
        self.assertGreater(before, 0)
        self._ctl("reset", "--yes")
        archived = list((self.root / "transcript-archive").glob("transcript-*.jsonl"))
        self.assertEqual(len(archived), 1)
        self.assertGreaterEqual(sum(1 for _ in archived[0].open(encoding="utf-8")), before)
        fresh = self.store.read_transcript(1000)
        self.assertEqual([e["kind"] for e in fresh], ["reset"])
        self.assertEqual(self.store.read().get("mode"), "fix")

    def test_task_wakes_the_writer(self) -> None:
        """#16: задача от человека обязана будить держателя записи.

        До правки `pairctl task` молча менял состояние: ни письма, ни заметки,
        ни wake-файла — писатель узнавал о новой работе только следующим
        случайным чтением. Проверяются обе стороны: добавление будит, закрытие
        будит, а без держателя записи будить некого и wake-файла быть не должно.
        """
        self._ctl("assign", "writer-agent")

        self._ctl("task", "wake me up")
        wake = self.root / "wake" / "writer-agent.json"
        self.assertTrue(wake.exists(), "новая задача обязана будить писателя")
        payload = json.loads(wake.read_text(encoding="utf-8"))
        self.assertIn("задача", payload["reasons"])

        self._ctl("task", "--done", "1")
        payload = json.loads(wake.read_text(encoding="utf-8"))
        self.assertIn("задача", payload["reasons"],
                      "закрытие задачи тоже меняет работу писателя")

    def test_task_drop_wakes_the_writer(self) -> None:
        """--drop идёт тем же кодом, что --done: писатель должен проснуться."""
        self._ctl("assign", "writer-agent")
        self._ctl("task", "drop me with reason")
        self._ctl("task", "--drop", "1", "--reason", "not needed")
        wake = self.root / "wake" / "writer-agent.json"
        self.assertTrue(wake.exists(), "дроп задачи обязан будить писателя")
        payload = json.loads(wake.read_text(encoding="utf-8"))
        self.assertIn("задача", payload["reasons"])

    def test_task_wake_failure_does_not_fail_the_command(self) -> None:
        """Находка pi: упавший record_wake не должен ронять cmd_task.

        Задача уже записана через mutate выше по коду; wake — best-effort,
        как в server.py (record_wake + wake_pending под try/except).
        """
        import unittest.mock
        self._ctl("assign", "writer-agent")
        with unittest.mock.patch("wake.record_wake", side_effect=OSError("disk full")):
            self.assertEqual(self._ctl("task", "wake fails but task lands"), 0)
        tasks = self.store.read()["tasks"]
        self.assertEqual(1, len(tasks))
        self.assertEqual("open", tasks[0]["status"])

    def test_task_without_writer_wakes_nobody(self) -> None:
        """Будеть некого — не будим никого и не падаем."""
        self._ctl("release")
        self._ctl("task", "work with no holder")
        wake_dir = self.root / "wake"
        leftovers = list(wake_dir.glob("*.json")) if wake_dir.is_dir() else []
        self.assertEqual([], leftovers,
                         "без держателя записи wake-файлов быть не должно")



class TestStateFlagPlacement(PairTestCase):
    def test_state_accepted_before_and_after_the_subcommand(self) -> None:
        self.assertEqual(main(["--state", str(self.root), "status"]), 0)
        self.assertEqual(main(["status", "--state", str(self.root)]), 0)
        self.assertEqual(main(["status", f"--state={self.root}"]), 0)

    def test_ctl_writes_land_in_the_requested_directory(self) -> None:
        main(["--state", str(self.root), "task", "resolve the retry storm"])
        self.assertEqual(Store(self.root).read()["task"], "resolve the retry storm")
        other = Path(self._tmp.name) / "elsewhere"
        main(["--state", str(other), "task", "unrelated"])
        self.assertEqual(Store(other).read()["task"], "unrelated")
        self.assertEqual(Store(self.root).read()["task"], "resolve the retry storm")


class TestRoundMode(PairTestCase):
    """`mode` decides what a round is for, so it has to survive both agents.

    The failure this guards against is an agent working from standing instructions that
    say "you are the writer, edit project files" while the round was opened precisely to
    change nothing. The mode lives in state so both sides read the same answer.
    """

    def _ctl(self, *args: str) -> int:
        """Run pairctl, turning argparse's SystemExit into the exit code it would give.

        argparse calls sys.exit itself, so an unknown subcommand argument raises rather
        than returning. A real shell sees an exit code; the test should see the same.
        """
        try:
            return main(["--state", str(self.root), *args])
        except SystemExit as exit_:
            return int(exit_.code or 0)

    def test_fix_is_the_default_and_both_agents_see_it(self) -> None:
        self.assertEqual(self.store.read()["mode"], "fix")

        for session in (self.writer, self.peer):
            state = session.tool_pair_state()
            self.assertEqual(state["mode"], "fix")
            self.assertIn("edits project files", state["mode_meaning"])

    def test_audit_mode_is_visible_to_both_roles(self) -> None:
        self._ctl("mode", "audit")

        for session in (self.writer, self.peer):
            state = session.tool_pair_state()
            self.assertEqual(state["mode"], "audit")
            # the point of the flag: the answer contradicts the default instruction
            self.assertIn("not edit", state["mode_meaning"].lower().replace("do not", "not"))

    def test_audit_text_allows_backlog_and_bans_code(self) -> None:
        """Карточка audit-mode-defines-itself-contradictorily: 'не меняй код,
        backlog можно и нужно' — в обоих местах (server.py + pair-guard.ts)."""
        self._ctl("mode", "audit")
        meaning = self.writer.tool_pair_state()["mode_meaning"]
        self.assertIn("docs/backlog", meaning)
        self.assertIn("allowed result", meaning)
        self.assertNotIn("Do not edit project files", meaning)
        guard = (Path(__file__).resolve().parent.parent.parent
                 / ".opencode" / "plugins" / "pair-guard.ts").read_text(encoding="utf-8")
        audit_block = guard[guard.index("if (audit)"):guard.index("// The verifier")]
        self.assertIn("No code gets changed", audit_block)
        self.assertIn("allowed result, not a code change", audit_block)
        # Вне audit-блока фраза легитимна (строка критика: не-писатель файлы не правит).
        self.assertNotIn("Do not edit project files", audit_block)

    def test_audit_refuses_a_patch_and_records_the_refusal(self) -> None:
        self._ctl("mode", "audit")
        # nobody holds authority, so the non-writer is the one who would propose a patch
        self.assertRefused(self.peer.tool_pair_patch_reserve, contains="audit round")

        # via invoke(), the way a real client calls it, the refusal lands on the trail
        self.assertRefused(
            lambda: self.peer.invoke("pair_patch_reserve", {}), contains="audit round"
        )
        refusals = [e for e in self.store.read_transcript() if e["kind"] == "refusal"]
        self.assertTrue(refusals, "a refusal nobody can see is not enforced")
        self.assertIn("audit round", refusals[0]["reason"].lower())

    def test_audit_does_not_release_authority_it_never_had(self) -> None:
        self._ctl("mode", "audit")
        self.writer.tool_pair_claim()

        # the mode changes the job, not who holds authority: phases still need a writer
        state = self.writer.tool_pair_state()
        self.assertTrue(state["you_are_writer"])
        self.assertEqual(state["writer"], "writer-agent")

    def test_mode_can_go_back_to_fix_and_patches_work_again(self) -> None:
        self._ctl("mode", "audit")
        self.assertRefused(self.peer.tool_pair_patch_reserve, contains="audit round")

        self._ctl("mode", "fix")
        self.assertEqual(self.store.read()["mode"], "fix")
        path = self.peer.tool_pair_patch_reserve()["path"]
        self.addCleanup(Path(path).unlink, missing_ok=True)
        self.assertTrue(Path(path).exists())

    def test_unknown_mode_is_refused(self) -> None:
        # argparse rejects it before the server sees it, so this exits 2 rather than
        # raising: an unknown mode must not be written into shared state
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self._ctl("mode", "review"), 2)
        self.assertEqual(self.store.read()["mode"], "fix")

    def test_unknown_mode_written_straight_into_state_is_reported_as_fix(self) -> None:
        # `_load` merges over the defaults, so a hand-edited or newer-server state keeps
        # whatever it had. An unrecognised mode must not be read as permission to edit:
        # only the literal "audit" changes the rules, everything else is a fix round.
        self.store.mutate(lambda state: state.__setitem__("mode", "review"))
        self.assertEqual(self.peer.tool_pair_state()["mode"], "fix")
        self.assertEqual(self.writer.tool_pair_claim()["already_held"], False)
        self.assertIn("path", self.peer.tool_pair_patch_reserve())

    def test_mode_change_is_on_the_audit_trail(self) -> None:
        self._ctl("mode", "audit")
        entries = [e for e in self.store.read_transcript() if e["kind"] == "mode"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["mode"], "audit")
        self.assertEqual(entries[0]["previous"], "fix")
        self.assertEqual(entries[0]["role"], "human")

    def test_every_permission_tool_consults_the_mode(self) -> None:
        # A test above only proves one tool checks. The risk in a feature this size is a
        # second permission tool that never heard of `mode`, and the omission reads as
        # correct behaviour until somebody runs an audit round for real. So: every tool
        # that carries a permission rule is named here, and each one is checked.
        self._ctl("mode", "audit")

        # the writer, who is refused patches by authority — the mode must refuse first,
        # otherwise the reason an agent gets is "you hold authority", which is not why
        self.writer.tool_pair_claim()
        self.assertRefused(self.writer.tool_pair_patch_reserve, contains="audit")

        # and the non-writer, refused by mode alone
        self.assertRefused(self.peer.tool_pair_patch_reserve, contains="audit")

    def test_audit_mode_does_not_block_notes_or_messaging(self) -> None:
        # The other half of the same risk: a mode that refuses too much is just as broken
        # as one that refuses too little. In an audit round the notes ARE the deliverable.
        self._ctl("mode", "audit")

        note = self.writer.tool_pair_note(kind="finding-F-01", text="store.py:1 — гонка")
        self.assertTrue(Path(note["path"]).exists())

        self.writer.tool_pair_send(to="reviewer-agent", text="находка F-01")
        self.assertEqual(len(self.peer.tool_pair_read()["messages"]), 1)

    def test_claim_note_does_not_tell_a_writer_to_edit_in_an_audit_round(self) -> None:
        # `pair_claim` is the first call a writer makes and its return value is the one
        # thing it reads about what it just became. "Edit project files directly" there
        # contradicts the mode, and pair-guard cannot fix it: it only rewrites the
        # standing brief, not a tool result.
        self._ctl("mode", "audit")
        note = self.writer.tool_pair_claim()["note"]
        self.assertNotIn("edit project files directly", note.lower())
        self.assertIn("audit", note.lower())

    def test_mode_is_visible_in_the_overview_tables(self) -> None:
        # `pairctl all` is where a human looks when they have forgotten which kind of
        # round a project is in. A mode nobody can see is a mode somebody forgets and
        # then wonders why the server refused a patch.
        store = Store(self.root)
        store.mutate(lambda state: state.update({"phase": "implement", "writer": "opencode"}))
        self.assertEqual(project_row(self.root)["mode"], "fix")

        self._ctl("mode", "audit")
        self.assertEqual(project_row(self.root)["mode"], "audit")
        self.assertIn("AUDIT", render_table([project_row(self.root)]))

    def test_control_surface_reports_the_mode(self) -> None:
        # redirect stderr too: argparse writes its usage message there, and a test that
        # leaks it into the test output is noise that hides a real failure
        self._ctl("mode", "audit")

        # `mode` prints a human line and `status` prints JSON, so the buffer holds both;
        # take the JSON object rather than the whole thing
        buffer, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(errors):
            self._ctl("status")
        payload = json.loads(buffer.getvalue()[buffer.getvalue().index("{") :])
        self.assertEqual(payload["mode"], "audit")

if __name__ == "__main__":
    unittest.main(verbosity=2)
