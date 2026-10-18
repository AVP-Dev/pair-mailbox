#!/usr/bin/env python3
"""Close-path gates: human checkpoint, evidence, autonomy, allowed defaults."""

from __future__ import annotations

import contextlib
import ast
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
from shutil import which as shutil_which

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pair_test_base import PAIRCHAT, SERVER, PairTestCase  # noqa: E402
from pairctl_read import project_row, render_table  # noqa: E402
from server import (  # noqa: E402
    AUTONOMIES,
    MAX_READ_WAIT_SECONDS,
    NOTE_LINE_CAP,
    NOTE_READ_MAX,
    PairSession,
    ProtocolError,
    Store,
    done_blockers,
    main,
    resolve_autonomy,
)

# The digest ceiling duplicates on purpose: the test must fall when the server
# raises its own constant above the checked one, not quietly rewrite it to match.
DIGEST_TOKEN_BUDGET_FOR_TEST = 500

def fresh_seen() -> str:
    """A fresh timestamp: the seed knowingly younger than any expiry TTL.

    Needed by the seeds of stale panes/dead processes: their intent is "drop the
    pane but keep the record", while year-old last_seen now honestly expires on
    TTL (19.3). Age runs random there; freshness pins the subject.
    """
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class TestHumanCheckpoint(PairTestCase):
    """`pairctl ok` meant "plan approved" and closed the round on verify."""

    def test_ok_given_on_plan_does_not_close_on_verify(self) -> None:
        self.set_autonomy("semi")
        self.writer.tool_pair_claim(reason="writer")
        self.store.mutate(lambda st: st.setdefault("human_ok", []).append(
            {"round": int(st.get("round", 0)), "at": "now", "phase": "plan"}))
        self.write_plan(int(self.store.read().get("round", 0)))
        self.ack_plan(int(self.store.read().get("round", 0)))
        for phase in ("plan", "implement", "verify"):
            self.writer.tool_pair_advance(phase)
        self.record_test_evidence()
        self.assertRefused(self.writer.tool_pair_advance, "done", contains="pairctl ok")

    def test_ok_given_on_verify_closes(self) -> None:
        self.writer.tool_pair_claim(reason="writer")
        self.advance_phases()
        self.record_test_evidence()
        self.human_ok()
        self.assertEqual("done", self.writer.tool_pair_advance("done")["phase"])

    def test_a_revoke_takes_the_ok_back(self) -> None:
        self.set_autonomy("semi")
        self.writer.tool_pair_claim(reason="writer")
        self.advance_phases()
        self.record_test_evidence()
        self.human_ok()
        self.store.mutate(lambda st: st.setdefault("human_ok", []).append(
            {"round": int(st.get("round", 0)), "at": "now", "phase": "verify", "revoke": True}))
        self.assertRefused(self.writer.tool_pair_advance, "done", contains="pairctl ok")


class TestEvidenceGate(PairTestCase):
    """The round-close gate. Model confidence in own estimates sits near 100% almost
    always, so "all happy" never passes for proof, and a round nobody checked must
    stay open."""

    def setUp(self) -> None:
        super().setUp()
        self.writer.tool_pair_state()
        self.peer.tool_pair_state()
        self.writer.tool_pair_claim(reason="writer")
        self.advance_phases()


    def require_rolekeys(self):
        """`rolekeys` or the test skip with no PyNaCl.

        Signing is an OPTIONAL function, and the server already proclaims it: importing
        rolekeys in try/except and fail-opening with no key
        (server.py:1850, 2367, 2492, 2722). No PyNaCl means no breakage but a lawful
        configuration.

        Tests used to import rolekeys directly, and on macOS with zero pip packages
        from CommandLineTools the install fell with two ModuleNotFoundErrors on top of
        dozens of others. No signature ever checked there — it just blocked the install.
        """
        try:
            import rolekeys
        except ImportError as exc:
            self.skipTest(f"PyNaCl missing ({exc.name}): signing off, "
                          f"the server runs fail-open")
        return rolekeys

    def test_done_is_refused_without_evidence(self) -> None:
        self.human_ok()
        error = self.assertRefused(self.writer.tool_pair_advance, "done",
                                   contains="no passing evidence")
        self.assertIn("pair_evidence", error.hint + str(error))
        self.assertEqual(self.store.read()["phase"], "verify")

    def test_evidence_opens_the_gate(self) -> None:
        self.record_test_evidence()
        self.human_ok()
        self.assertEqual(self.writer.tool_pair_advance("done")["phase"], "done")

    def test_a_verdict_is_not_a_test_run(self) -> None:
        """The gate used to ask "any successful record at all", and `kind="manual",
        value="looks good"` answered that question. A record indistinguishable from
        a run closed the round instead of holding it."""
        self.writer.tool_pair_evidence(kind="manual", value="looks good to me", ok=True)
        self.human_ok()
        error = self.assertRefused(self.writer.tool_pair_advance, "done")
        self.assertIn("no passing test run", str(error))
        self.assertIn('kind="test"', str(error))

    def test_a_passing_test_run_satisfies_that_gate(self) -> None:
        self.record_test_evidence()
        self.human_ok()
        self.assertEqual(self.writer.tool_pair_advance("done")["phase"], "done")

    def test_done_refused_without_writer_ground_truth(self) -> None:
        """[R82] Tests with no gate runs and another run mean refusal (R81)."""
        self.writer.tool_pair_evidence(kind="test", value="117 passed, exit 0", ok=True)
        self.human_ok()
        self.assertRefused(self.writer.tool_pair_advance, "done", contains="ground-truth")
        self.peer.tool_pair_evidence(kind="ground-truth", value="PASS", ok=True)
        self.assertRefused(self.writer.tool_pair_advance, "done", contains="ground-truth")

    def test_an_audit_round_does_not_demand_a_test_run(self) -> None:
        """In mode=audit the work result is findings in notes, and no test run may exist
        in substance. The gate must never demand what this mode never does."""
        self.store.mutate(lambda state: state.__setitem__("mode", "audit"))
        self.writer.tool_pair_evidence(kind="manual", value="28 findings, all confirmed", ok=True)
        self.human_ok()
        self.assertEqual(self.writer.tool_pair_advance("done")["phase"], "done")

    def test_a_failing_test_run_is_not_replaced_by_a_passing_lint_run(self) -> None:
        """The "last run decides" rule counts by kind, not by the whole round: green lint
        never cancels a red test."""
        self.record_test_evidence("3 failed, exit 1", ok=False)
        self.writer.tool_pair_evidence(kind="lint", value="clean, exit 0", ok=True)
        self.human_ok()
        self.assertRefused(self.writer.tool_pair_advance, "done", contains="failing evidence")

    def test_a_flood_of_later_entries_cannot_evict_this_rounds_failure(self) -> None:
        """The journal cut on EVIDENCE_KEEP=50 with no round regard, while the gate reads
        state, not the transcript. Fifty records past a failure — and a round with
        red tests closed, because the journal overflowed."""
        self.record_test_evidence("3 failed, exit 1", ok=False)
        for index in range(60):
            self.writer.tool_pair_evidence(kind="commit", value=f"noise {index}", ok=True)
        entries = self.store.read()["evidence"]
        failing = [e for e in entries if not e.get("ok") and e.get("kind") == "test"]
        self.assertTrue(failing, "the current-round failure record must survive")
        self.human_ok()
        self.assertRefused(self.writer.tool_pair_advance, "done", contains="failing evidence")

    def test_evidence_of_old_rounds_is_still_trimmed(self) -> None:
        """The band for past rounds stays, or state grows without bound."""
        self.store.mutate(lambda state: state.__setitem__("evidence", [
            {"round": 0, "kind": "commit", "value": f"old {i}", "ok": True, "at": "then"}
            for i in range(120)
        ]))
        self.record_test_evidence()
        entries = self.store.read()["evidence"]
        self.assertLessEqual(sum(1 for e in entries if e["round"] == 0), 50)
        self.assertTrue(any(e["round"] != 0 for e in entries), "the current-round record in place")

    def test_failing_evidence_blocks_closing(self) -> None:
        """A red test, honestly recorded, must block the round close. Or writing that all
        is well runs easier than fixing."""
        self.record_test_evidence("3 failed, exit 1", ok=False)
        self.human_ok()
        self.assertRefused(self.writer.tool_pair_advance, "done", contains="failing evidence")

    def test_failing_then_passing_lets_the_round_close(self) -> None:
        self.record_test_evidence("3 failed, exit 1", ok=False)
        self.record_test_evidence("120 passed, exit 0")
        self.human_ok()
        self.assertEqual(self.writer.tool_pair_advance("done")["phase"], "done")

    def test_evidence_from_a_previous_round_does_not_count(self) -> None:
        self.record_test_evidence("117 passed", ok=True)
        self.human_ok()
        self.writer.tool_pair_advance("done")
        # Reopen: a new round starts, the old evidence stays on the books.
        self.peer.tool_pair_advance("implement")
        self.advance_phases()
        self.assertRefused(self.writer.tool_pair_advance, "done",
                           contains="no passing evidence")
        # The human's word does not carry either: it was said about that round.
        self.record_test_evidence("117 passed", ok=True)
        # The level declares explicitly: these tests used to lean on the default and fell
        # on its change — the semi contract pinned by none of them.
        self.set_autonomy("semi")
        self.assertRefused(self.writer.tool_pair_advance, "done",
                           contains="pairctl ok")

    def test_observe_mode_needs_evidence_too(self) -> None:
        """observe used to switch all gates off at once, reading as a side effect, not a
        decision: the safest mode was the only one where the round closed on nothing
        at all. By decision the early exit is gone — observe means "do less", not
        "never count".
        """
        self.set_autonomy("observe")
        self.assertRefused(self.writer.tool_pair_advance, "done", contains="passing evidence")

    def test_auto_needs_no_human_word(self) -> None:
        self.set_autonomy("auto")
        self.record_test_evidence()
        self.assertEqual(self.writer.tool_pair_advance("done")["phase"], "done")

    def test_semi_needs_the_human_word(self) -> None:
        self.set_autonomy("semi")
        self.record_test_evidence()
        error = self.assertRefused(self.writer.tool_pair_advance, "done",
                                   contains="pairctl ok")
        self.assertIn("autonomy auto", str(error))

    def test_peer_evidence_counts_but_cannot_close_a_fix_round(self) -> None:
        """Anyone may bring the proof — but the gate guarding against "closing on words"
        never closes on another "ok".

        The critic with no right to run tests used to record kind=test, ok=True and
        satisfy the writer gate. Now the run must be the writer one.
        """
        self.peer.tool_pair_evidence(kind="test", value="192 passed, exit 0")
        self.human_ok()
        self.assertRefused(self.writer.tool_pair_advance, "done", contains="from the writer")
        self.record_test_evidence()
        self.assertEqual(self.writer.tool_pair_advance("done")["phase"], "done")

    def test_unknown_kind_is_refused(self) -> None:
        self.assertRefused(self.writer.tool_pair_evidence, "vibes", "good", contains="kind")

    def test_empty_value_is_refused(self) -> None:
        self.assertRefused(self.writer.tool_pair_evidence, "test", "  ", contains="value")

    def test_signed_evidence_carries_a_verifying_envelope(self) -> None:
        """A keyed fact signs with the round:kind:task:value envelope (3.1)."""
        rolekeys = self.require_rolekeys()
        rolekeys.generate(self.store.root, self.store.project_root, "writer-agent")
        self.record_test_evidence()
        items = [i for i in self.store.read()["evidence"] if i.get("round") == self.store.read()["round"]]
        test_item = [i for i in items if i.get("kind") == "test"][0]
        env = test_item.get("envelope")
        self.assertIsInstance(env, dict, "a keyed fact signs")
        vk = rolekeys.load_verify_key(self.store.root, "writer-agent")
        assert vk is not None
        text = f"{test_item['round']}:{test_item['kind']}:{test_item['task'] or ''}:{test_item['value']}"
        self.assertTrue(rolekeys.verify_envelope(vk, env, text))

    def test_tampered_evidence_blocks_the_close(self) -> None:
        """A swapped signed fact shows to the gate: bad_signature, the round never closes."""
        rolekeys = self.require_rolekeys()
        rolekeys.generate(self.store.root, self.store.project_root, "writer-agent")
        self.record_test_evidence()
        def tamper(state):
            for item in state.get("evidence", []):
                if item.get("round") == state.get("round"):
                    item["value"] = "swapped"
            return {"tampered": True}
        self.store.mutate(tamper)
        self.human_ok()
        error = self.assertRefused(self.writer.tool_pair_advance, "done", contains="bad signature")
        kinds = [e.get("kind") for e in self.store.read_transcript(0)]
        self.assertIn("bad_signature", kinds, "the swap shows in the transcript")

    def test_unsigned_evidence_still_closes_fail_open(self) -> None:
        """With no key (no .pair/keys) an unsigned fact closes as before."""
        self.record_test_evidence()
        items = [i for i in self.store.read()["evidence"] if i.get("round") == self.store.read()["round"]]
        self.assertNotIn("envelope", items[0], "with no key there is no signature")
        self.human_ok()
        self.assertEqual(self.writer.tool_pair_advance("done")["phase"], "done")


    def test_state_says_nothing_is_awaited_from_the_human_in_auto(self) -> None:
        """The default flip side: in auto nobody must await the human word, and the agent
        must see it in state, not know it by memory."""
        state = self.writer.tool_pair_state()
        self.assertEqual(state["autonomy"], "auto")
        self.assertFalse(state["awaiting_human_ok"])
        self.record_test_evidence()
        self.assertEqual(self.writer.tool_pair_advance("done")["phase"], "done")

    def test_state_reports_autonomy_and_its_meaning(self) -> None:
        """The default is pinned: exactly what the human set, and nothing pinned it before
        — six tests silently leaned on it."""
        state = self.writer.tool_pair_state()
        self.assertEqual(state["autonomy"], "auto")
        self.assertIn("close itself", state["autonomy_meaning"])
        for level in AUTONOMIES:
            self.store.mutate(lambda st, lv=level: st.__setitem__("autonomy", lv))
            meaning = self.writer.tool_pair_state()["autonomy_meaning"]
            self.assertTrue(meaning and "unattended" in meaning.lower() or "driving" in meaning.lower(),
                            f"{level}: the level meaning must explain, never stay silent")

    def test_unknown_autonomy_reads_as_observe(self) -> None:
        """A newer-build state must never grant more rights."""
        self.store.mutate(lambda state: state.__setitem__("autonomy", "turbo"))
        self.assertEqual(self.writer.tool_pair_state()["autonomy"], "observe")
        self.record_test_evidence()
        self.assertEqual(self.writer.tool_pair_advance("done")["phase"], "done")


class TestAutonomyDefault(PairTestCase):
    """The default is auto — exactly what the human set: it gives a task, the pair
    works it alone and returns with one output.

    The default used to be semi, and a fresh project started asking for `ok` on
    verify nobody awaited. Tests leaning on it now declare the level explicitly —
    or a default change breaks tests unrelated to the default.
    """

    def test_a_fresh_project_starts_in_auto(self) -> None:
        self.assertEqual(self.store.read()["autonomy"], "auto")
        self.assertEqual(self.writer.tool_pair_state()["autonomy"], "auto")

    def test_reset_leaves_auto_and_not_observe(self) -> None:
        """A wipe used to leave observe — "wiping must never raise rights". But rights
        count against the default, and the human named auto the default. So observe
        after wipe stopped the pair against "work alone", forcing the human to notice.
        """
        self.set_autonomy("observe")
        self.assertEqual(main(["--state", str(self.root), "reset", "--yes"]), 0)
        self.assertEqual(self.store.read()["autonomy"], "auto")

    def test_a_missing_autonomy_key_reads_as_auto_not_semi(self) -> None:
        """A missing key means an old state file, not a plea to be careful. Reading it as
        semi meant stopping where the human never asked."""
        self.store.mutate(lambda state: state.pop("autonomy", None))
        self.assertEqual(self.writer.tool_pair_state()["autonomy"], "auto")

    def test_an_unknown_autonomy_key_still_reads_as_observe(self) -> None:
        """A new level from a newer build must never grant more rights than the human
        asked: unknown means observe, even with auto the default now."""
        self.store.mutate(lambda state: state.__setitem__("autonomy", "turbo"))
        self.assertEqual(self.writer.tool_pair_state()["autonomy"], "observe")


class TestAutonomyTriggers(PairTestCase):
    """The mode picks once at start, and the pick place must show: a sourceless level
    turns in a month into a dispute over "why this round closed itself"."""

    def resolve(self, flag: str | None = None, task: str = "") -> tuple[str, str]:
        return resolve_autonomy(self.root, task=task, flag=flag)

    def declare(self, text: str) -> None:
        (self.root / "autonomy").write_text(text, encoding="utf-8")

    def test_no_signal_at_all_means_auto(self) -> None:
        self.assertEqual(self.resolve(), ("auto", "default"))

    def test_a_file_sets_the_level(self) -> None:
        self.declare("semi")
        self.assertEqual(self.resolve(), ("semi", "file"))

    def test_a_flag_beats_the_file(self) -> None:
        self.declare("semi")
        self.assertEqual(self.resolve(flag="observe"), ("observe", "flag"))

    def test_a_task_line_beats_the_file(self) -> None:
        self.declare("observe")
        self.assertEqual(self.resolve(task="autonomy: semi — fix the test"),
                         ("semi", "task"))

    def test_the_flag_beats_the_task_line(self) -> None:
        self.assertEqual(self.resolve(flag="observe", task="autonomy: auto — fix"),
                         ("observe", "flag"))

    def test_the_task_line_must_start_the_task(self) -> None:
        """Or a prose word changes the pair powers: a task "move text about autonomy: semi"
        must never turn the pair semi-auto."""
        self.declare("auto")
        self.assertEqual(self.resolve(task="move text about autonomy: semi"),
                         ("auto", "file"))

    def test_a_junk_file_falls_through_instead_of_failing(self) -> None:
        """Refusing to launch beats starting at a level the human sees in `pairctl status`."""
        self.declare("turbo")
        self.assertEqual(self.resolve(), ("auto", "default"))

    def test_a_junk_flag_falls_through_to_the_next_source(self) -> None:
        self.declare("semi")
        self.assertEqual(self.resolve(flag="turbo"), ("semi", "file"))

    def test_the_launcher_writes_the_level_and_where_it_came_from(self) -> None:
        self.declare("semi")
        self.assertEqual(main(["--state", str(self.root), "launch-autonomy"]), 0)
        self.assertEqual(self.store.read()["autonomy"], "semi")
        entry = [e for e in self.store.read_transcript(20) if e.get("kind") == "autonomy"][-1]
        self.assertEqual(entry["via"], "file", "the decision source must land in the journal")

    def test_the_launcher_says_auto_and_where_it_came_from(self) -> None:
        self.assertEqual(main(["--state", str(self.root), "launch-autonomy"]), 0)
        self.assertEqual(self.store.read()["autonomy"], "auto")
        entry = [e for e in self.store.read_transcript(20) if e.get("kind") == "autonomy"][-1]
        self.assertEqual(entry["via"], "default")


class TestDefaultAllowlist(PairTestCase):
    """A missing settings file means the default list, not "confirm nothing".

    Found live: no `.pair/permissions.json` file exists, its path became the only
    pattern, missing the prompt text — and the supervisor confirmed zero requests.
    From outside that read as "the neighbour stopped helping", not a settings refusal.

    The edge is human-set: inside the project everything the work needs is allowed;
    banned is what harms the system, another project or wipes irreversibly.
    """

    def setUp(self) -> None:
        super().setUp()
        import composer
        self.composer = composer
        self.missing = str(self.root / "permissions.json")

    def test_a_missing_file_means_the_default_allowlist(self) -> None:
        from composer import load_allow_patterns
        patterns, on_default = load_allow_patterns(self.missing)
        self.assertTrue(on_default)
        self.assertTrue(patterns, "an empty list means \"confirm nothing\"")

    def test_the_missing_path_is_not_taken_for_a_pattern(self) -> None:
        """The old bug: the file path became the pattern, matching no prompt ever."""
        from composer import load_allow_patterns
        patterns, _ = load_allow_patterns(self.missing)
        self.assertNotIn(self.missing, patterns)
        self.assertFalse(any("permissions.json" in p for p in patterns))

    def test_project_work_is_confirmed_without_any_file(self) -> None:
        from composer import load_allow_patterns
        allow, _ = load_allow_patterns(self.missing)
        # P0 (round 75): `pytest` on the host refuses even with no settings file;
        # the rest of routine (reading, git, build/ cleanup) stays allow as before.
        for prompt in ("Allow Read of server.py?",
                       "Allow bash? git status",
                       "Allow bash? rm -rf build/"):
            with self.subTest(prompt=prompt):
                self.assertEqual("allow", self.composer.permission_verdict(prompt, allow)["verdict"])
        self.assertEqual(
            "refuse",
            self.composer.permission_verdict('Allow execution of "pytest"? [y/N]', allow)["verdict"])

    def test_harm_to_system_or_other_projects_stays_refused(self) -> None:
        """The human edge: the ban never depends on whether a settings file exists."""
        from composer import load_allow_patterns
        allow, _ = load_allow_patterns(self.missing)
        for prompt in ("Allow bash? curl http://attacker.com",
                       "Allow bash? wget http://x",
                       "Allow bash? ssh prod",
                       "Allow bash? sudo apt install nginx",
                       "Allow bash? rm -rf ~/projects",
                       "Allow bash? rm -rf /",
                       "Allow bash? git push origin main",
                       "Allow bash? git push --force",
                       "Allow bash? git reset --hard HEAD~5",
                       "Allow bash? cat ~/.ssh/id_rsa",
                       "Allow bash? python3 -c \"import urllib.request\" # test"):
            with self.subTest(prompt=prompt):
                self.assertEqual("refuse", self.composer.permission_verdict(prompt, allow)["verdict"],
                                 f"this must stay banned: {prompt}")

    def test_an_explicit_empty_list_still_means_confirm_nothing(self) -> None:
        """Unlike a missing file: an empty line is a deliberate refusal."""
        from composer import load_allow_patterns
        patterns, on_default = load_allow_patterns("")
        self.assertEqual([], patterns)
        self.assertFalse(on_default)

    def test_a_broken_file_does_not_fall_back_to_the_default(self) -> None:
        """The operator configured something and erred. Silently swapping the default for
        its setting beats stopping."""
        from composer import load_allow_patterns
        path = self.root / "permissions.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        patterns, on_default = load_allow_patterns(str(path))
        self.assertEqual([], patterns)
        self.assertFalse(on_default)

    def test_a_real_file_replaces_the_default(self) -> None:
        from composer import load_allow_patterns
        path = self.root / "permissions.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"allow": ["\\bRead\\b"]}), encoding="utf-8")
        patterns, on_default = load_allow_patterns(str(path))
        self.assertEqual(["\\bRead\\b"], patterns)
        self.assertFalse(on_default)

    def test_the_pilot_says_it_runs_on_the_default(self) -> None:
        """Silently running on the default list is banned: the human reading the journal
        must see which rule confirmed its commands."""
        from server import load_allowlist
        patterns, on_default = load_allowlist(self.root)
        self.assertTrue(on_default)
        self.assertTrue(patterns)
