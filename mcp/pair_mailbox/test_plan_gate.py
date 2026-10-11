"""Машинный гейт план → implement: файл, виза, исключения, само-виза.

Один тест-файл на один исходник: `plan_gate.py` -> `test_plan_gate.py`
(база PairTestCase даёт живую пару двух ролей на состоянии).
"""
from __future__ import annotations

import json
import time
import unittest
from pathlib import Path

from pair_test_base import PairTestCase
import plan_gate


def write_plan(state_dir: Path, round_number: int, sections: tuple = ("Goal", "Invariants", "Acceptance")) -> None:
    body = [f"round: {round_number}", ""]
    for section in sections:
        body += [f"## {section}", "текст", ""]
    (state_dir / plan_gate.PLAN_FILE).write_text("\n".join(body), encoding="utf-8")


def write_ack(note_dir: Path, role: str, round_number: int, at: float | None = None) -> None:
    note_dir.mkdir(parents=True, exist_ok=True)
    stamp = at if at is not None else time.time()
    header = ("## 2026-10-11T18:00:00+00:00 — " + role + "\n") if at is None else None
    if header is None:
        from datetime import datetime, timezone
        header = ("## " + datetime.fromtimestamp(stamp, tz=timezone.utc).isoformat()
                  + " — " + role + "\n")
    (note_dir / "verdict.md").write_text(
        header + f"\nplan-ack round {round_number}\n", encoding="utf-8")
    # mtime — источник правды для «не раньше плана»: двигаем заметку вперёд
    import os
    os.utime(note_dir / "verdict.md", (stamp, stamp))


class TestGateCases(PairTestCase):
    """Провод гейта через настоящий tool_pair_advance."""

    def setUp(self) -> None:
        super().setUp()
        self.writer.tool_pair_claim(reason="writer")

    def advance_to_plan(self) -> None:
        self.writer.tool_pair_advance("plan")

    def test_no_plan_refuses(self) -> None:
        self.advance_to_plan()
        error = self.assertRefused(self.writer.tool_pair_advance, "implement")
        self.assertIn("plan.md не читается", str(error))

    def test_wrong_round_marker_refuses(self) -> None:
        self.advance_to_plan()
        write_plan(self.store.root, 42)
        error = self.assertRefused(self.writer.tool_pair_advance, "implement")
        self.assertIn("round: 42", str(error))

    def test_missing_sections_refuse(self) -> None:
        self.advance_to_plan()
        write_plan(self.store.root, 1, sections=("Goal",))
        error = self.assertRefused(self.writer.tool_pair_advance, "implement")
        self.assertIn("invariants", str(error))

    def test_plan_plus_critic_ack_passes(self) -> None:
        self.advance_to_plan()
        write_plan(self.store.root, 1)
        write_ack(self.store.root / "notes", "reviewer-agent", 1, at=time.time() + 1)
        result = self.writer.tool_pair_advance("implement")
        self.assertEqual("implement", result["phase"])

    def test_self_ack_does_not_count(self) -> None:
        """Виза от самого писателя — обход гейта: роль автора проверяется."""
        self.advance_to_plan()
        write_plan(self.store.root, 1)
        write_ack(self.store.root / "notes", "writer-agent", 1, at=time.time() + 1)
        error = self.assertRefused(self.writer.tool_pair_advance, "implement")
        self.assertIn("окно", str(error))

    def test_timeout_passes_without_open_requests(self) -> None:
        self.advance_to_plan()
        write_plan(self.store.root, 1)
        hours = plan_gate._ack_timeout_hours(self.store.root)
        stale = time.time() - hours * 3600 - 60
        import os
        os.utime(self.store.root / plan_gate.PLAN_FILE, (stale, stale))
        self.assertEqual("implement", self.writer.tool_pair_advance("implement")["phase"])

    def test_timeout_blocked_while_request_open(self) -> None:
        """Открытый запрос писателя не даёт замечанию истечь незамеченным."""
        self.advance_to_plan()
        write_plan(self.store.root, 1)
        import os
        stale = time.time() - 25 * 3600
        os.utime(self.store.root / plan_gate.PLAN_FILE, (stale, stale))
        self.peer.tool_pair_send("writer-agent", "возражение по плану", expects_reply=False)
        self.writer.tool_pair_send("reviewer-agent", "согласуй план", expects_reply=True)
        error = self.assertRefused(self.writer.tool_pair_advance, "implement")
        self.assertIn("запрос", str(error))

    def test_reopen_is_exempt(self) -> None:
        """done → implement (переоткрытие по находке) вставало бы без плана:
        self-lock — именно тот риск, из-за которого exempt существует."""
        self.ready_and_close()
        self.writer.tool_pair_advance("implement")
        self.assertEqual("implement", self.store.read()["phase"])

    def test_audit_needs_artifact_only(self) -> None:
        self.writer.tool_pair_advance("plan")
        self.store.mutate(lambda st: st.update(mode="audit"))
        write_plan(self.store.root, 1)
        self.assertEqual("implement", self.writer.tool_pair_advance("implement")["phase"])

    def ready_and_close(self) -> None:
        write_plan(self.store.root, self.store.read()["round"])
        write_ack(self.store.root / "notes", "reviewer-agent",
                  self.store.read()["round"], at=time.time() + 1)
        for phase in ("plan", "implement", "verify"):
            self.writer.tool_pair_advance(phase)
        self.record_test_evidence()
        self.writer.tool_pair_advance("done")


if __name__ == "__main__":
    unittest.main(verbosity=2)
