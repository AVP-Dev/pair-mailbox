"""Tests for the budget ceiling (S2): machine-counted metrics, ceilings, refusal and reset.

One test file per source: `budget.py` -> `test_budget.py`
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
import unittest

from pair_test_base import PairTestCase
import budget
from server import ProtocolError


class TestBudgetMetrics(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_default_budget_loaded_when_no_file(self) -> None:
        cfg = budget.load_budget(self.state_dir)
        self.assertEqual(cfg["max_messages"], 200)
        self.assertEqual(cfg["max_advances"], 10)
        self.assertEqual(cfg["max_hours"], 24.0)
        self.assertEqual(cfg["max_restarts"], 10)

    def test_custom_budget_override(self) -> None:
        b_file = self.state_dir / budget.BUDGET_FILE
        b_file.write_text(json.dumps({"max_messages": 50, "max_hours": 12.5}), encoding="utf-8")
        cfg = budget.load_budget(self.state_dir)
        self.assertEqual(cfg["max_messages"], 50)
        self.assertEqual(cfg["max_advances"], 10)
        self.assertEqual(cfg["max_hours"], 12.5)
        self.assertEqual(cfg["max_restarts"], 10)

    def test_metrics_empty_transcript(self) -> None:
        st = {"round": 1}
        m = budget.collect_metrics(self.state_dir, st)
        self.assertEqual(m["messages"], 0)
        self.assertEqual(m["advances"], 0)
        self.assertEqual(m["hours"], 0.0)
        self.assertEqual(m["restarts"], 0)

    def test_metrics_counting_in_round(self) -> None:
        t_file = self.state_dir / "transcript.jsonl"
        t0 = datetime(2026, 10, 11, 10, 0, 0, tzinfo=timezone.utc)
        entries = [
            # The prior round (closed in done)
            {"ts": t0.isoformat(), "kind": "advance", "role": "agy", "phase": "done", "previous": "verify"},
            # Round 2 start (reopened from done)
            {"ts": (t0 + timedelta(minutes=5)).isoformat(), "kind": "advance", "role": "agy", "phase": "implement", "previous": "done", "reopened": True},
            # Messages in round 2
            {"ts": (t0 + timedelta(minutes=10)).isoformat(), "kind": "send", "role": "agy", "to": "omp"},
            {"ts": (t0 + timedelta(minutes=15)).isoformat(), "kind": "deliver", "role": "omp", "to": "agy"},
            # Moving to verify
            {"ts": (t0 + timedelta(minutes=20)).isoformat(), "kind": "advance", "role": "agy", "phase": "verify", "previous": "implement"},
        ]
        t_file.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")

        # Pilot restarts
        r_file = self.state_dir / ".restarts-agy"
        r_file.write_text(f"{(t0 + timedelta(minutes=12)).timestamp():.0f}", encoding="utf-8")

        st = {"round": 2}
        now = t0 + timedelta(hours=2)
        m = budget.collect_metrics(self.state_dir, st, now=now)
        self.assertEqual(m["messages"], 2)
        self.assertEqual(m["advances"], 1)  # advance to verify
        self.assertAlmostEqual(m["hours"], 1.92, places=2)
        self.assertEqual(m["restarts"], 1)

    def test_reset_marker_resets_metrics(self) -> None:
        t_file = self.state_dir / "transcript.jsonl"
        t0 = datetime(2026, 10, 11, 10, 0, 0, tzinfo=timezone.utc)
        entries = [
            {"ts": t0.isoformat(), "kind": "advance", "role": "agy", "phase": "implement", "previous": "done", "reopened": True},
            {"ts": (t0 + timedelta(minutes=10)).isoformat(), "kind": "send", "role": "agy"},
        ]
        t_file.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")

        # Resetting at 10:15
        reset_time = t0 + timedelta(minutes=15)
        budget.reset_budget(self.state_dir, round_number=1)
        # Rewriting reset_at to an exact time for determinism
        (self.state_dir / budget.RESET_FILE).write_text(
            json.dumps({"round": 1, "reset_at": reset_time.isoformat()}), encoding="utf-8"
        )

        # A new message past the reset
        with t_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": (t0 + timedelta(minutes=20)).isoformat(), "kind": "send", "role": "agy"}) + "\n")

        now = t0 + timedelta(minutes=25)
        m = budget.collect_metrics(self.state_dir, {"round": 1}, now=now)
        self.assertTrue(m["reset_applied"])
        self.assertEqual(m["messages"], 1)  # Only what stands past 10:15
        self.assertAlmostEqual(m["hours"], 0.17, places=2)


class TestBudgetServerIntegration(PairTestCase):
    "Integration with tool_pair_advance and the pairctl budget CLI."

    def setUp(self) -> None:
        super().setUp()
        self.writer.tool_pair_claim(reason="writer")

    def test_forward_advance_refused_on_budget_breach(self) -> None:
        # Setting a 1-message limit
        (self.store.root / budget.BUDGET_FILE).write_text(
            json.dumps({"max_messages": 1}), encoding="utf-8"
        )
        self.writer.tool_pair_advance("plan")
        self.write_plan(1)
        self.ack_plan(1)
        self.writer.tool_pair_advance("implement")

        # Sending 2 messages to break the limit
        self.writer.tool_pair_send(to="reviewer-agent", subject="work", text="msg 1")
        self.writer.tool_pair_send(to="reviewer-agent", subject="work", text="msg 2")

        # A forward implement -> verify step must refuse on budget
        with self.assertRaises(ProtocolError) as ctx:
            self.writer.tool_pair_advance("verify")
        self.assertIn("round budget breached", str(ctx.exception))
        self.assertIn("messages:", str(ctx.exception))

    def test_backward_step_allowed_even_on_budget_breach(self) -> None:
        (self.store.root / budget.BUDGET_FILE).write_text(
            json.dumps({"max_messages": 1}), encoding="utf-8"
        )
        self.writer.tool_pair_advance("plan")
        self.write_plan(1)
        self.ack_plan(1)
        self.writer.tool_pair_advance("implement")

        self.writer.tool_pair_send(to="reviewer-agent", subject="work", text="msg 1")
        self.writer.tool_pair_send(to="reviewer-agent", subject="work", text="msg 2")

        # A backward implement -> plan step MUST STAY ALLOWED
        res = self.writer.tool_pair_advance("plan")
        self.assertEqual(res["phase"], "plan")

    def test_done_allowed_even_on_budget_breach(self) -> None:
        (self.store.root / budget.BUDGET_FILE).write_text(
            json.dumps({"max_messages": 1}), encoding="utf-8"
        )
        self.writer.tool_pair_advance("plan")
        self.write_plan(1)
        self.ack_plan(1)
        self.writer.tool_pair_advance("implement")

        self.writer.tool_pair_send(to="reviewer-agent", subject="work", text="msg 1")
        self.writer.tool_pair_send(to="reviewer-agent", subject="work", text="msg 2")

        # Resetting budget to reach verify
        budget.reset_budget(self.store.root, self.writer.store.read().get("round", 0))
        self.writer.tool_pair_advance("verify")

        # Exhausting budget again
        self.writer.tool_pair_send(to="reviewer-agent", subject="work", text="msg 3")
        self.writer.tool_pair_send(to="reviewer-agent", subject="work", text="msg 4")

        # Writing evidence
        self.record_test_evidence("tests pass")
        # Reading all incoming
        self.writer.tool_pair_read()

        # The verify -> done move MUST STAY ALLOWED past budget!
        res = self.writer.tool_pair_advance("done")
        self.assertEqual(res["phase"], "done")

    def test_cli_budget_status_and_reset(self) -> None:
        from pairctl_cli import ctl_main

        # Checking ctl_main budget
        code = ctl_main(["budget", "--json"], store=self.store)
        self.assertEqual(code, 0)

        # Checking ctl_main budget --reset
        code = ctl_main(["budget", "--reset"], store=self.store)
        self.assertEqual(code, 0)
        self.assertTrue((self.store.root / budget.RESET_FILE).is_file())


if __name__ == "__main__":
    unittest.main()
