"""Tests for manifest.py (round 94 C9): server-derived, cross-checked, no agent fields.
Run: python3 mcp/pair_mailbox/test_manifest.py
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import manifest
from manifest import ManifestRefused, cross_check, derive_manifest


def _state(**over) -> dict:
    base: dict = {"round": 94, "mode": "fix", "writer": "omp",
                  "tasks": [], "evidence": [], "verdicts": []}
    base.update(over)
    return base


class TestDerive(unittest.TestCase):
    def test_request_from_closed_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sd = Path(tmp)
            st = _state(tasks=[{"id": 1, "text": "real work", "status": "done",
                                "round": 94, "closed_round": 94}])
            m = derive_manifest(sd, st)
        self.assertEqual(m["request"], ["real work"])
        self.assertEqual(m["round"], 94)

    def test_tasks_bound_by_close_round(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sd = Path(tmp)
            st = _state(tasks=[{"id": 1, "text": "w", "status": "done",
                                "round": 93, "closed_round": 94}])
            m = derive_manifest(sd, st)
        self.assertEqual(len(m["tasks_closed"]), 1)

    def test_verdict_binds_note_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sd = Path(tmp)
            (sd / "notes").mkdir()
            (sd / "notes" / "verdict-x.md").write_text("## h\n\nAPPROVED\n", encoding="utf-8")
            st = _state(verdicts=[{"role": "c", "kind": "verdict-x", "at": "t"}])
            m = derive_manifest(sd, st)
        self.assertEqual(m["verdicts"][0]["note"], ".pair/notes/verdict-x.md")


class TestCrossCheck(unittest.TestCase):
    def test_empty_commits_with_branch_commits_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sd = Path(tmp)
            st = _state()
            m = derive_manifest(sd, st)
            m["commits"] = []
            with mock.patch.object(manifest, "_git_commits",
                                   return_value=[{"sha": "abc", "subject": "x"}]):
                gaps = cross_check(m, st, sd)
        self.assertTrue(gaps)

    def test_empty_manifest_on_empty_sources_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sd = Path(tmp)
            st = _state()
            m = derive_manifest(sd, st)
            with mock.patch.object(manifest, "_git_commits", return_value=[]):
                gaps = cross_check(m, st, sd)
        self.assertEqual(gaps, [])

    def test_evidence_gap_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sd = Path(tmp)
            st = _state(evidence=[{"kind": "test", "ok": True, "value": "v",
                                   "role": "omp", "round": 94}])
            m = derive_manifest(sd, st)
            m["evidence"] = []
            gaps = cross_check(m, st, sd)
        self.assertTrue(gaps)


class TestNegativeAndMethod(unittest.TestCase):
    def test_negative_aggregates_markers(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sd = Path(tmp)
            (sd / "notes").mkdir()
            (sd / "notes" / "verdict-a.md").write_text(
                "## h\n\nAll good.\n\nWhat this does NOT cover: the merge.\n",
                encoding="utf-8")
            st = _state()
            m = derive_manifest(sd, st)
        self.assertTrue(any("verdict-a.md" in n["where"] for n in m["negative"]))

    def test_method_unstated_without_markers(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sd = Path(tmp)
            st = _state(evidence=[{"kind": "test", "ok": True, "value": "all good",
                                   "role": "omp", "round": 94}])
            m = derive_manifest(sd, st)
        self.assertEqual(m["evidence"][0]["method"], ["unstated"])

    def test_method_derived_from_markers(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sd = Path(tmp)
            st = _state(evidence=[{"kind": "test", "ok": True,
                                   "value": "full gate green, mutation-red",
                                   "role": "omp", "round": 94}])
            m = derive_manifest(sd, st)
        self.assertIn("mutation", m["evidence"][0]["method"])

    def test_transcript_tasks_gap_refuses(self) -> None:
        """task_closed in journal, manifest claims none: gap (round-94 live case)."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sd = Path(tmp)
            (sd / "transcript.jsonl").write_text(
                '{"ts": "2026-10-14T21:01:45+00:00", "kind": "task_closed",'
                ' "task_id": 107, "status": "done"}\n', encoding="utf-8")
            st = _state()
            m = derive_manifest(sd, st)
            m["tasks_closed"] = []
            with mock.patch.object(manifest, "_git_commits", return_value=[]):
                gaps = cross_check(m, st, sd)
        self.assertTrue(any("#107" in g for g in gaps))


class TestNegativeRuMarkers(unittest.TestCase):
    def test_russian_markers_found(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sd = Path(tmp)
            (sd / "notes").mkdir()
            (sd / "notes" / "verdict-ru.md").write_text(
                "## h\n\nЧего не покрывает: слияние.\n", encoding="utf-8")
            st = _state()
            m = derive_manifest(sd, st)
        self.assertTrue(any("verdict-ru.md" in n["where"] for n in m["negative"]))


class TestRecheckDelta(unittest.TestCase):
    """Round 94 C10: delta-zero is the only pass."""

    def test_identical_manifests_no_delta(self) -> None:
        sys_path = Path(__file__).resolve().parent.parent.parent / "bin"
        import importlib.util as _ilu
        from importlib.machinery import SourceFileLoader
        loader = SourceFileLoader("pairreport_mod", str(sys_path / "pairreport"))
        spec = _ilu.spec_from_loader("pairreport_mod", loader)
        mod = _ilu.module_from_spec(spec)
        loader.exec_module(mod)
        m = {"request": ["w"], "commits": [], "tasks_closed": [],
             "tasks_dropped": [], "verdicts": [], "evidence": [],
             "mode": "fix", "writer": "omp", "plan": {"path": "p", "sha": "s"}}
        self.assertEqual(mod._diff_manifests(m, dict(m)), [])

    def test_added_evidence_is_delta(self) -> None:
        import importlib.util as _ilu
        from importlib.machinery import SourceFileLoader
        from pathlib import Path as _P
        loader = SourceFileLoader("pairreport_mod",
                                  str(_P(__file__).resolve().parent.parent.parent / "bin" / "pairreport"))
        spec = _ilu.spec_from_loader("pairreport_mod", loader)
        mod = _ilu.module_from_spec(spec)
        loader.exec_module(mod)
        old = {"request": [], "commits": [], "tasks_closed": [],
               "tasks_dropped": [], "verdicts": [], "evidence": [],
               "mode": "fix", "writer": "omp", "plan": {}}
        new = dict(old, evidence=[{"kind": "test"}])
        delta = mod._diff_manifests(old, new)
        self.assertTrue(any("evidence" in line for line in delta))


if __name__ == "__main__":
    unittest.main(verbosity=2)
