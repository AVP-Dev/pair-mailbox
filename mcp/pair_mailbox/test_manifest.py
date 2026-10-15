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

    def test_task_closed_in_archive_is_seen(self) -> None:
        """Rotation blindness (round-94 live case): task_closed sits in
        transcript-archive/1.jsonl while the active file is cut."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sd = Path(tmp)
            (sd / "transcript-archive").mkdir()
            (sd / "transcript-archive" / "1.jsonl").write_text(
                '{"ts": "2026-10-14T20:00:00+00:00", "kind": "advance",'
                ' "phase": "done"}\n'
                '{"ts": "2026-10-14T21:01:45+00:00", "kind": "task_closed",'
                ' "task_id": 107, "status": "done"}\n', encoding="utf-8")
            (sd / "transcript.jsonl").write_text(
                '{"ts": "2026-10-14T21:02:00+00:00", "kind": "evidence",'
                ' "evidence_kind": "test", "ok": true}\n', encoding="utf-8")
            from manifest import transcript_closed_ids
            self.assertIn(107, transcript_closed_ids(sd))

    def test_window_anchor_survives_rotation(self) -> None:
        """The last done lives in the archive: the window still starts there,
        not at the rotation cut."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sd = Path(tmp)
            (sd / "transcript-archive").mkdir()
            (sd / "transcript-archive" / "2.jsonl").write_text(
                '{"ts": "2026-10-14T20:00:00+00:00", "kind": "advance",'
                ' "phase": "done"}\n', encoding="utf-8")
            (sd / "transcript-archive" / "1.jsonl").write_text(
                '{"ts": "2026-10-14T21:01:45+00:00", "kind": "task_closed",'
                ' "task_id": 107, "status": "done"}\n', encoding="utf-8")
            (sd / "transcript.jsonl").write_text(
                '{"ts": "2026-10-14T21:05:00+00:00", "kind": "task_closed",'
                ' "task_id": 108, "status": "done"}\n', encoding="utf-8")
            from manifest import transcript_closed_ids
            ids = transcript_closed_ids(sd)
            self.assertIn(107, ids)
            self.assertIn(108, ids)



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


class TestLabeledBinding(unittest.TestCase):
    def test_closed_round_wins(self) -> None:
        from manifest import tasks_closed_in_round
        tasks = [{"id": 1, "status": "done", "round": 93, "closed_round": 94}]
        rows = tasks_closed_in_round(tasks, 94, set())
        self.assertEqual(rows[0]["bound_by"], "closed_round")

    def test_pre_d2_fallback_labeled(self) -> None:
        from manifest import tasks_closed_in_round
        tasks = [{"id": 1, "status": "done", "round": 94}]
        rows = tasks_closed_in_round(tasks, 94, set())
        self.assertIn("pre-D2", rows[0]["bound_by"])

    def test_stale_server_close_via_transcript(self) -> None:
        """The round-94 live case: no closed_round, journal has the close."""
        from manifest import tasks_closed_in_round
        tasks = [{"id": 107, "status": "done", "round": 93}]
        rows = tasks_closed_in_round(tasks, 94, {107})
        self.assertEqual(len(rows), 1)
        self.assertIn("stale-server", rows[0]["bound_by"])

    def test_unrelated_task_excluded(self) -> None:
        from manifest import tasks_closed_in_round
        tasks = [{"id": 5, "status": "done", "round": 91, "closed_round": 92}]
        self.assertEqual(tasks_closed_in_round(tasks, 94, {5}), [])


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

    def test_cannot_prove_exits_2_without_merge_evidence(self) -> None:
        """Round-96 verify should-fix: the recheck exit-2 path gets a
        durable hermetic test, not just live runs. Closed round, ceiling
        survives, window holds rows, no merge evidence → exit 2."""
        import importlib.util as _ilu
        from importlib.machinery import SourceFileLoader
        loader = SourceFileLoader(
            "pairreport_mod2",
            str(Path(__file__).resolve().parent.parent.parent / "bin" / "pairreport"))
        spec = _ilu.spec_from_loader("pairreport_mod2", loader)
        mod = _ilu.module_from_spec(spec)
        loader.exec_module(mod)
        with tempfile.TemporaryDirectory() as tmp:
            sd = Path(tmp)
            (sd / "transcript-archive").mkdir()
            (sd / "transcript-archive" / "1.jsonl").write_text(
                '{"ts": "2026-10-14T20:35:08+00:00", "kind": "advance", "phase": "done"}\n'
                '{"ts": "2026-10-14T21:01:45+00:00", "kind": "task_closed", "task_id": 107}\n',
                encoding="utf-8")
            (sd / "transcript.jsonl").write_text(
                '{"ts": "2026-10-14T21:44:53+00:00", "kind": "advance", "phase": "done"}\n',
                encoding="utf-8")
            (sd / "rounds").mkdir()
            (sd / "rounds" / "94.json").write_text(
                json.dumps({"round": 94, "commits": [], "tasks_closed": []}), encoding="utf-8")
            (sd / "state.json").write_text(json.dumps(
                {"round": 96,
                 "tasks": [{"id": 107, "round": 94, "status": "done",
                            "closed_at": "2026-10-14T21:01:45+00:00"}],
                 "evidence": [], "verdicts": []}), encoding="utf-8")
            rc = mod.recheck(sd, 94)
        self.assertEqual(rc, 2)

    def test_open_round_never_cannot_prove(self) -> None:
        """Twin: the OPEN round must not exit 2 — the guard is `not is_open`.
        Dropping that check would silently turn every current-round recheck
        into exit 2; this pins the guard."""
        import importlib.util as _ilu
        from importlib.machinery import SourceFileLoader
        loader = SourceFileLoader(
            "pairreport_mod3",
            str(Path(__file__).resolve().parent.parent.parent / "bin" / "pairreport"))
        spec = _ilu.spec_from_loader("pairreport_mod3", loader)
        mod = _ilu.module_from_spec(spec)
        loader.exec_module(mod)
        with tempfile.TemporaryDirectory() as tmp:
            sd = Path(tmp)
            (sd / "transcript.jsonl").write_text("", encoding="utf-8")
            (sd / "rounds").mkdir()
            (sd / "rounds" / "96.json").write_text(
                json.dumps({"round": 96, "commits": [], "tasks_closed": []}), encoding="utf-8")
            (sd / "state.json").write_text(json.dumps(
                {"round": 96, "tasks": [], "evidence": [], "verdicts": []}), encoding="utf-8")
            import contextlib
            import io
            with contextlib.redirect_stdout(io.StringIO()):
                rc = mod.recheck(sd, 96)
        self.assertNotEqual(rc, 2)

class TestRoundWindow96(unittest.TestCase):
    """Round 96: the manifest and the recheck see their OWN round.

    Window from N-stamped rows (evidence round=N + closed_at of N-labeled
    tasks) over the united stream; closed-round commits from recorded merge
    evidence, never the live diff; unrecoverable, never empty.
    """

    def _stream(self, sd: Path) -> None:
        (sd / "transcript-archive").mkdir()
        (sd / "transcript-archive" / "1.jsonl").write_text(
            '{"ts": "2026-10-14T20:35:08+00:00", "kind": "advance", "phase": "done"}\n'
            '{"ts": "2026-10-14T21:01:45+00:00", "kind": "task_closed", "task_id": 107}\n'
            '{"ts": "2026-10-14T21:04:20+00:00", "kind": "task_closed", "task_id": 108}\n',
            encoding="utf-8")
        (sd / "transcript.jsonl").write_text(
            '{"ts": "2026-10-14T21:43:40+00:00", "kind": "evidence", "round": 94}\n'
            '{"ts": "2026-10-14T21:44:53+00:00", "kind": "advance", "phase": "done"}\n'
            '{"ts": "2026-10-15T16:51:46+00:00", "kind": "task_closed", "task_id": 111}\n',
            encoding="utf-8")

    def test_window_names_own_round_not_last_done(self) -> None:
        """C1: round-94 window is anchor..ceiling, task 111 (round 95) excluded."""
        from manifest import _round_bounds
        with tempfile.TemporaryDirectory() as tmp:
            sd = Path(tmp)
            self._stream(sd)
            st = _state(round=96)
            anchor, ceiling = _round_bounds(sd, 94, st)
        self.assertEqual(anchor, "2026-10-14T20:35:08+00:00")
        self.assertEqual(ceiling, "2026-10-14T21:44:53+00:00")

    def test_closed_round_ids_exclude_later_round(self) -> None:
        """task_closed 107+108 in window, 111 (after ceiling) out."""
        from manifest import transcript_closed_ids
        with tempfile.TemporaryDirectory() as tmp:
            sd = Path(tmp)
            self._stream(sd)
            st = _state(round=96)
            ids = transcript_closed_ids(sd, 94, [], st)
        self.assertEqual(ids, {107, 108})

    def test_closed_commits_from_recorded_evidence(self) -> None:
        """C2/B2: range names the tip only — the pre-merge anchor is not ours."""
        from manifest import _round_commits_from_evidence
        st = _state(round=96, evidence=[
            {"round": 94, "evidence_kind": "commit",
             "value": "PR #40 merged: 771fd77 Round 94 D1,D2,C9,C10, origin/master b6bfb65..771fd77"},
            {"round": 89, "evidence_kind": "pr",
             "value": "https://github.com/AVP-Dev/pair-mailbox/pull/36 round89-deliver-round88 -> master"}])
        commits94 = _round_commits_from_evidence(st, 94)
        self.assertIsNotNone(commits94)
        assert commits94 is not None
        self.assertEqual([c["sha"] for c in commits94], ["771fd77"])
        self.assertIsNone(_round_commits_from_evidence(st, 89))

    def test_empty_with_anchor_refuses(self) -> None:
        """Acceptance #3: manifest [] with recorded anchor commits refuses."""
        with tempfile.TemporaryDirectory() as tmp:
            sd = Path(tmp)
            self._stream(sd)
            st = _state(round=96, evidence=[
                {"round": 94, "evidence_kind": "commit", "value": "merged: abc1234 x"}])
            m = derive_manifest(sd, st, 94)
            m["commits"] = []
            gaps = cross_check(m, st, sd)
        self.assertTrue(any("recorded merge evidence" in g for g in gaps))

    def test_open_round_live_branch_still_refuses(self) -> None:
        """C4: the open-round branch guards the round being closed now."""
        with tempfile.TemporaryDirectory() as tmp:
            sd = Path(tmp)
            st = _state(round=96)
            m = derive_manifest(sd, st)
            m["commits"] = []
            with mock.patch.object(manifest, "_git_commits",
                                   return_value=[{"sha": "abc", "subject": "x"}]):
                gaps = cross_check(m, st, sd)
        self.assertTrue(any("on branch" in g for g in gaps))

    def test_none_with_rows_refuses_never_empty(self) -> None:
        """B1: closed round, ceiling survives, no merge evidence — None
        refuses via the window-rows gap, never collapses to []."""
        with tempfile.TemporaryDirectory() as tmp:
            sd = Path(tmp)
            self._stream(sd)
            st = _state(round=96)
            m = derive_manifest(sd, st, 94)
            self.assertIsNone(m["commits"])
            self.assertTrue(m["commits_unrecoverable"])
            gaps = cross_check(m, st, sd)
        self.assertTrue(any("cannot-prove" in g for g in gaps))

    def test_verdicts_filtered_by_window(self) -> None:
        """M1: state verdicts filter by at inside the window, note rides along."""
        with tempfile.TemporaryDirectory() as tmp:
            sd = Path(tmp)
            self._stream(sd)
            (sd / "notes").mkdir()
            (sd / "notes" / "verdict-v-in.md").write_text("x", encoding="utf-8")
            st = _state(round=96, verdicts=[
                {"role": "c", "kind": "v-old", "at": "2026-10-14T20:00:00+00:00",
                 "note": ".pair/notes/v-old.md"},
                {"role": "c", "kind": "verdict-v-in", "at": "2026-10-14T21:00:00+00:00",
                 "note": ".pair/notes/verdict-v-in.md"}])
            m = derive_manifest(sd, st, 94)
        self.assertEqual([v["kind"] for v in m["verdicts"]], ["verdict-v-in"])
        self.assertEqual(m["verdicts"][0]["note"], ".pair/notes/verdict-v-in.md")


if __name__ == "__main__":
    unittest.main(verbosity=2)
