#!/usr/bin/env python3
"""Write authority, roster persistence, patch flow — who may write and how a patch travels."""

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


class TestWriteAuthority(PairTestCase):
    def test_claim_and_release(self) -> None:
        self.writer.tool_pair_state()  # register
        self.peer.tool_pair_state()

        result = self.writer.tool_pair_claim()
        self.assertEqual(result["writer"], "writer-agent")
        self.assertFalse(result["already_held"])

        self.assertEqual(self.store.read()["writer"], "writer-agent")

        released = self.writer.tool_pair_release()
        self.assertTrue(released["released"])
        self.assertIsNone(self.store.read()["writer"])

    def test_second_claim_is_refused(self) -> None:
        self.writer.tool_pair_state()
        self.peer.tool_pair_state()
        self.writer.tool_pair_claim()

        error = self.assertRefused(self.peer.tool_pair_claim, contains="held by")
        self.assertIn("pairctl", str(error.hint))
        self.assertEqual(self.store.read()["writer"], "writer-agent")

    def test_peer_cannot_release_someone_elses_authority(self) -> None:
        self.writer.tool_pair_state()
        self.peer.tool_pair_state()
        self.writer.tool_pair_claim()

        self.assertRefused(self.peer.tool_pair_release, contains="does not hold")
        self.assertEqual(self.store.read()["writer"], "writer-agent")

    def test_reclaim_is_idempotent_not_a_round_bump(self) -> None:
        self.writer.tool_pair_state()
        self.peer.tool_pair_state()
        first = self.writer.tool_pair_claim()
        again = self.writer.tool_pair_claim()
        self.assertTrue(again["already_held"])
        self.assertEqual(first["round"], again["round"])

    def test_claim_after_release_makes_the_other_role_writer(self) -> None:
        self.writer.tool_pair_state()
        self.peer.tool_pair_state()
        self.writer.tool_pair_claim()
        self.writer.tool_pair_release()
        result = self.peer.tool_pair_claim()
        self.assertEqual(result["writer"], "reviewer-agent")

    def test_a_claim_says_why(self) -> None:
        """The user asked whether picking the role deliberately beats the race.

        Answer: the race stays (or a human is needed and autonomy ends), but the
        claim now explains itself. The neighbour sees the argument and may disagree.
        """
        self.writer.tool_pair_state()
        self.peer.tool_pair_state()
        self.writer.tool_pair_claim(reason="I already found where to fix it: retry in the client")

        self.assertEqual(self.store.read()["writer_reason"], "I already found where to fix it: retry in the client")
        # the neighbour sees the cause, not only the fact
        self.assertEqual(
            self.peer.tool_pair_state()["writer_reason"],
            "I already found where to fix it: retry in the client",
        )

    def test_the_loser_sees_the_winners_reason_in_the_refusal(self) -> None:
        self.writer.tool_pair_state()
        self.peer.tool_pair_state()
        self.writer.tool_pair_claim(reason="I already found where to fix it: retry in the client")

        error = self.assertRefused(self.peer.tool_pair_claim, contains="retry in the client")
        # a refusal must hint what to do next, not only state the fact
        self.assertIn("pair_release", str(error.hint))

    def test_a_claim_without_a_reason_is_allowed_but_records_nothing(self) -> None:
        # the tool must never break on an empty line, but "why" stays empty
        self.writer.tool_pair_state()
        self.writer.tool_pair_claim()
        self.assertEqual(self.store.read().get("writer_reason", ""), "")

    def test_release_can_say_why_and_the_peer_then_claims(self) -> None:
        # an honest handover: the holder itself decided the second fits better
        self.writer.tool_pair_state()
        self.peer.tool_pair_state()
        self.writer.tool_pair_claim(reason="trying first")
        self.writer.tool_pair_release(why="I lack the test-db access, agy has it")

        self.assertEqual(self.store.read()["writer_reason"], "")
        self.assertEqual(self.peer.tool_pair_claim(reason="I have the access")["writer"],
                         "reviewer-agent")

    def test_release_with_a_reason_leaves_it_in_the_audit_trail(self) -> None:
        self.writer.tool_pair_state()
        self.writer.tool_pair_claim(reason="first")
        self.writer.tool_pair_release(why="hand over to the neighbour")

        events = [e for e in self.store.read_transcript(0) if e["kind"] == "release"]
        self.assertEqual(events[-1].get("reason"), "hand over to the neighbour")

    def test_human_assignment_is_marked_as_the_humans_choice(self) -> None:
        # or the exchange never tells a race from a human decision
        self.writer.tool_pair_state()
        self.peer.tool_pair_state()
        main(["--state", str(self.root), "assign", "reviewer-agent"])

        self.assertEqual(self.store.read()["writer_reason"], "assigned by human")

    def test_why_command_reports_the_reason(self) -> None:
        self.writer.tool_pair_state()
        self.writer.tool_pair_claim(reason="I figured out the retry stream")
        # PAIR_SCAN awaits .pair paths, while setUp creates state right in self.root
        os.environ["PAIR_SCAN"] = str(self.root)
        cwd = os.getcwd()
        os.chdir(self.root)
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                code = main(["why"])
        finally:
            del os.environ["PAIR_SCAN"]
            os.chdir(cwd)
        self.assertEqual(code, 0)
        self.assertIn("figured out the retry stream", out.getvalue())


class TestPersistentRoles(PairTestCase):
    """Standing policy: the human once names who always writes, and the race for
    write authority vanishes. Without it every round spends a move disputing, and
    a dispute is a reason to stall mid-work."""

    def pin(self, writer: str, critic: str) -> None:
        self.store.mutate(lambda st: st.__setitem__("policy", {"writer": writer, "critic": critic}))

    def test_designated_writer_takes_authority_without_a_race(self) -> None:
        self.pin("writer-agent", "reviewer-agent")
        result = self.writer.tool_pair_claim(reason="I write by policy")
        self.assertEqual(result["writer"], "writer-agent")
        self.assertTrue(result.get("by_policy"))
        self.assertEqual(self.store.read()["writer"], "writer-agent")

    def test_designated_critic_is_refused_and_told_why(self) -> None:
        self.pin("writer-agent", "reviewer-agent")
        error = self.assertRefused(self.peer.tool_pair_claim, contains="critic")
        self.assertIn("writer-agent", str(error))
        self.assertIn("pairctl roles --swap", error.hint)

    def test_policy_does_not_steal_authority_from_a_live_writer(self) -> None:
        self.writer.tool_pair_state()
        self.writer.tool_pair_claim(reason="already writing")
        self.pin("reviewer-agent", "writer-agent")
        # The policy writer is not whoever already holds the right: no race runs for it,
        # but silently taking the right from a live writer is banned too.
        state = self.store.read()
        self.assertEqual(state["writer"], "writer-agent")

    def test_designated_writer_takes_over_a_dead_holder(self) -> None:
        self.peer.tool_pair_claim(reason="holding")
        self.pin("writer-agent", "reviewer-agent")
        self.store.mutate(lambda st: (
            st["roles"]["reviewer-agent"].__setitem__("pid", 999999999),
            st["roles"]["reviewer-agent"].__setitem__("last_seen", "2000-01-01T00:00:00+00:00"),
        ))
        result = self.writer.tool_pair_claim(reason="continuing in a new CLI")
        self.assertEqual(result["writer"], "writer-agent")
        self.assertTrue(result.get("by_policy"))

    def test_anyone_may_not_take_over_a_live_holder(self) -> None:
        self.pin("writer-agent", "reviewer-agent")
        self.writer.tool_pair_claim(reason="holding")
        self.assertRefused(self.peer.tool_pair_claim, contains="role-policy")
        self.assertEqual(self.store.read()["writer"], "writer-agent")

    def test_any_live_role_takes_over_a_dead_holder_without_policy(self) -> None:
        self.writer.tool_pair_claim(reason="holding")
        self.store.mutate(lambda st: (
            st["roles"]["writer-agent"].__setitem__("pid", 999999999),
            st["roles"]["writer-agent"].__setitem__("last_seen", "2000-01-01T00:00:00+00:00"),
        ))
        result = self.peer.tool_pair_claim(reason="continuing in a new CLI")
        self.assertEqual(result["writer"], "reviewer-agent")
        self.assertTrue(result.get("took_over"))

    def test_critic_never_bypasses_designated_writer_on_dead_holder(self) -> None:
        # seq 466: holder_dead lifted the critic refusal, and it took the right around the
        # policy. The critic refusal stands always; a dead-holder grab belongs to the named only.
        self.peer.tool_pair_claim(reason="holding")
        self.pin("writer-agent", "reviewer-agent")
        self.store.mutate(lambda st: (
            st["roles"]["reviewer-agent"].__setitem__("pid", 999999999),
            st["roles"]["reviewer-agent"].__setitem__("last_seen", "2000-01-01T00:00:00+00:00"),
        ))
        self.assertRefused(self.peer.tool_pair_claim, contains="role-policy")
        self.assertEqual(self.store.read()["writer"], "reviewer-agent")

    def test_no_policy_keeps_the_race(self) -> None:
        self.writer.tool_pair_state()
        result = self.writer.tool_pair_claim(reason="read the task first")
        self.assertFalse(result.get("by_policy", False))
        self.assertEqual(result["writer"], "writer-agent")

    def test_state_says_what_you_are(self) -> None:
        self.pin("writer-agent", "reviewer-agent")
        self.assertIn("writer by standing policy", self.writer.tool_pair_state()["my_standing"])
        # Writer-pin: the critic stays nameless — "critic (writes …)", with no "standing".
        standing = self.peer.tool_pair_state()["my_standing"]
        self.assertIn("critic", standing)
        self.assertIn("writer-agent", standing)

    def test_state_separates_designated_from_holder(self) -> None:
        self.pin("writer-agent", "reviewer-agent")
        self.writer.tool_pair_claim(reason="holding")
        standing = self.peer.tool_pair_state()["my_standing"]
        self.assertIn("critic", standing)
        self.assertIn("writer-agent", standing)
        self.assertFalse(self.peer.tool_pair_state()["you_are_writer"])

    def test_standing_forms_three_and_four_follow_the_canon(self) -> None:
        """Pin my_standing forms 3-4 (seq 1193): no other test pins them.

        Form 3 (designated writer, holder holds): assigned-writer wording with
        the live-refusal note. Form 4 (critic view): critic (writes X, holds Y).
        """
        self.pin("writer-agent", "reviewer-agent")
        self.store.mutate(lambda st: st.__setitem__("writer", "reviewer-agent"))
        standing = self.writer.tool_pair_state()["my_standing"]
        self.assertIn("assigned writer", standing)
        self.assertIn("refuses only the live", standing)
        peer_standing = self.peer.tool_pair_state()["my_standing"]
        self.assertIn("critic (writes", peer_standing)
        self.assertIn("holds", peer_standing)

    def test_claim_after_release_still_follows_the_policy(self) -> None:
        self.pin("writer-agent", "reviewer-agent")
        self.writer.tool_pair_claim()
        self.store.mutate(lambda st: st.__setitem__("writer", None))
        result = self.writer.tool_pair_claim()
        self.assertEqual(result["writer"], "writer-agent")

    def test_cli_roles_rejects_missing_writer(self) -> None:
        # Writer-pin: policy with no writer never writes; lone --critic creates no pin.
        proc = subprocess.run(
            [sys.executable, str(SERVER), "--state", str(self.root), "roles",
             "--critic", "only-critic"],
            capture_output=True, text=True, timeout=30,
        )
        self.assertNotIn("policy", self.store.read())

    def test_cli_swap_needs_a_second_live_role(self) -> None:
        # Swap moves the writer to the second live roster entry; with no second — a refusal,
        # not a silent pin (writer-pin stores no critic).
        self.writer.tool_pair_state()
        self.peer.tool_pair_state()
        self.pin("writer-agent", "reviewer-agent")
        proc = subprocess.run(
            [sys.executable, str(SERVER), "--state", str(self.root), "roles", "--swap"],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.store.read()["policy"], {"writer": "reviewer-agent"})

    def test_cli_clear_returns_to_the_race(self) -> None:
        self.pin("writer-agent", "reviewer-agent")
        proc = subprocess.run(
            [sys.executable, str(SERVER), "--state", str(self.root), "roles", "--clear"],
            capture_output=True, text=True, timeout=30,
        )
        self.assertNotIn("policy", self.store.read())



class TestPatchFlow(PairTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.writer.tool_pair_state()
        self.peer.tool_pair_state()
        self.writer.tool_pair_claim()

    def test_writer_may_not_reserve_a_patch(self) -> None:
        self.assertRefused(self.writer.tool_pair_patch_reserve, contains="write authority")

    def test_full_handoff(self) -> None:
        reserved = self.peer.tool_pair_patch_reserve()
        path = reserved["path"]

        mode = oct(os.stat(path).st_mode & 0o777)
        self.assertEqual(mode, "0o600")

        Path(path).write_text("--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n", encoding="utf-8")

        sealed = self.peer.tool_pair_patch_seal(path)
        self.assertEqual(len(sealed["sha256"]), 64)

        verified = self.writer.tool_pair_patch_verify(path)
        self.assertTrue(verified["match"])
        self.assertEqual(verified["recorded_sha256"], verified["actual_sha256"])

        released = self.peer.tool_pair_patch_release(path)
        self.assertTrue(released["deleted"])
        self.assertFalse(Path(path).exists())

    def test_tampered_patch_fails_verification(self) -> None:
        path = self.peer.tool_pair_patch_reserve()["path"]
        Path(path).write_text("original\n", encoding="utf-8")
        self.peer.tool_pair_patch_seal(path)

        Path(path).write_text("tampered\n", encoding="utf-8")
        verified = self.writer.tool_pair_patch_verify(path)

        self.assertFalse(verified["match"])
        self.assertIn("STOP", verified["verdict"])

    def test_non_writer_may_not_verify(self) -> None:
        path = self.peer.tool_pair_patch_reserve()["path"]
        Path(path).write_text("x\n", encoding="utf-8")
        self.peer.tool_pair_patch_seal(path)
        self.assertRefused(self.peer.tool_pair_patch_verify, path, contains="only the writer")

    def test_globs_are_rejected_everywhere(self) -> None:
        path = self.peer.tool_pair_patch_reserve()["path"]
        Path(path).write_text("x\n", encoding="utf-8")
        self.peer.tool_pair_patch_seal(path)

        self.assertRefused(self.peer.tool_pair_patch_seal, path + "*", contains="glob")
        self.assertRefused(self.peer.tool_pair_patch_release, "/tmp/pair-*", contains="glob")
        self.assertRefused(self.writer.tool_pair_patch_verify, path[:-1] + "?", contains="glob")
        # the real file survived every rejected attempt
        self.assertTrue(Path(path).exists())

    def test_cannot_seal_or_release_a_path_you_never_reserved(self) -> None:
        stranger = Path(tempfile.mkdtemp()) / "not-ours.patch"
        stranger.write_text("x\n", encoding="utf-8")
        self.assertRefused(self.peer.tool_pair_patch_seal, str(stranger), contains="not reserved")
        self.assertRefused(self.peer.tool_pair_patch_release, str(stranger), contains="not reserved")

    def test_cannot_touch_another_roles_patch(self) -> None:
        path = self.peer.tool_pair_patch_reserve()["path"]
        Path(path).write_text("x\n", encoding="utf-8")
        self.peer.tool_pair_patch_seal(path)

        # a third role exists purely to try the same trick
        intruder = PairSession("intruder-agent", self.store)
        intruder.tool_pair_state()
        self.assertRefused(intruder.tool_pair_patch_seal, path, contains="belongs to")
        self.assertRefused(intruder.tool_pair_patch_release, path, contains="belongs to")

    def test_seal_requires_a_sealed_hash(self) -> None:
        path = self.peer.tool_pair_patch_reserve()["path"]
        Path(path).write_text("x\n", encoding="utf-8")
        self.assertRefused(self.writer.tool_pair_patch_verify, path, contains="never sealed")

    def test_propose_refuses_empty_globs_and_writer(self) -> None:
        """One command — the same edges as six steps: empty, globs, project escape, and the
        writer with its direct tree access."""
        self.assertRefused(self.peer.tool_pair_propose, [], contains="non-empty")
        self.assertRefused(self.peer.tool_pair_propose, ["*.py"], contains="globs")
        self.assertRefused(self.peer.tool_pair_propose, ["/abs/x.py"], contains="inside")
        self.assertRefused(self.peer.tool_pair_propose, ["../x.py"], contains="inside")
        self.assertRefused(self.writer.tool_pair_propose, ["x.py"], contains="must not")

    def test_propose_builds_seals_and_delivers(self) -> None:
        """The full cycle in one command: worktree diff, seal, the writer letter.

        A live git root in a temp dir: pair state rises right in it (`repo/.pair`),
        the critic edits a file, propose builds the diff itself. Everything the tool
        promises is checked: the patch file with a diff, seal with a hash, the writer
        letter with path+hash, and an open obligation.
        """
        import subprocess

        from server import PairSession

        repo = Path(self._tmp.name) / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=str(repo), check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=str(repo), check=True)
        target = repo / "note.txt"
        target.write_text("one\n", encoding="utf-8")
        subprocess.run(["git", "add", "note.txt"], cwd=str(repo), check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=str(repo), check=True)
        target.write_text("two\n", encoding="utf-8")
        store_repo = __import__("server").Store(repo / ".pair")
        critic = PairSession("reviewer-agent", store_repo)
        writer = PairSession("writer-agent", store_repo)
        critic.tool_pair_state()
        writer.tool_pair_state()
        writer.tool_pair_claim(reason="writer")
        result = critic.tool_pair_propose(["note.txt"], subject="look")
        body = Path(result["path"]).read_text(encoding="utf-8")
        self.assertIn("-one", body)
        self.assertIn("+two", body)
        self.assertEqual(len(result["sha256"]), 64)
        letter = writer.tool_pair_read(limit=10)["messages"][-1]
        self.assertIn(result["path"], letter["text"])
        self.assertIn(result["sha256"], letter["text"])
        obligations = list(store_repo.obligations_root.glob("*.json"))
        self.assertEqual(len(obligations), 1)


class TestPatchAccounting(PairTestCase):
    """A patch nobody reported applied must show.

    Before this `applied` recorded at reservation and never changed, so "applied"
    and "lying since last night" read identical in state.
    """

    def setUp(self) -> None:
        super().setUp()
        self.writer.tool_pair_state()
        self.peer.tool_pair_state()
        self.writer.tool_pair_claim(reason="writer")

    def sealed_patch(self, body: str = "--- a\n+++ b\n@@ -1 +1 @@\n-x\n+y\n") -> str:
        path = self.peer.tool_pair_patch_reserve()["path"]
        Path(path).write_text(body, encoding="utf-8")
        self.peer.tool_pair_patch_seal(path)
        return path

    def ready(self) -> None:
        self.advance_phases()
        self.record_test_evidence()
        self.human_ok()

    def test_applied_patch_stops_blocking(self) -> None:
        path = self.sealed_patch()
        self.assertTrue(self.writer.tool_pair_patch_verify(path)["match"])
        result = self.writer.tool_pair_patch_applied(path, ok=True, note="commit abc1234")
        self.assertTrue(result["applied"])
        self.assertEqual(result["still_unresolved"], [])
        self.ready()
        self.assertEqual(self.writer.tool_pair_advance("done")["phase"], "done")

    def test_unapplied_patch_blocks_closing_and_names_itself(self) -> None:
        path = self.sealed_patch()
        self.ready()
        error = self.assertRefused(self.writer.tool_pair_advance, "done")
        self.assertIn(Path(path).name, str(error))
        self.assertIn("pair_patch_applied", str(error))

    def test_failed_apply_still_blocks(self) -> None:
        """A failed apply also means "unresolved": the round must never close over a patch
        that never landed."""
        path = self.sealed_patch()
        self.writer.tool_pair_patch_verify(path)
        self.writer.tool_pair_patch_applied(path, ok=False, note="hunk 3 rejected")
        self.ready()
        self.assertRefused(self.writer.tool_pair_advance, "done")

    def test_release_clears_the_blocker(self) -> None:
        path = self.sealed_patch()
        self.peer.tool_pair_patch_release(path)
        self.ready()
        self.assertEqual(self.writer.tool_pair_advance("done")["phase"], "done")

    def test_release_of_a_sealed_patch_is_auditable(self) -> None:
        """A rejected patch must stay in history: "dropped" and "forgotten" differ, and the
        report must tell them apart."""
        path = self.sealed_patch()
        self.peer.tool_pair_patch_release(path)
        events = [e for e in self.store.read_transcript() if e.get("kind") == "patch_dropped"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["sha256"],
                         [e for e in self.store.read_transcript()
                          if e.get("kind") == "patch_seal"][0]["sha256"])
        self.assertTrue(events[0]["sealed"])

    def test_the_writer_can_drop_a_patch_they_did_not_propose(self) -> None:
        """The deadlock I stepped into live.

        The neighbour patch hangs, the neighbour left, and the round never closes:
        `applied` demands `verified_match` while `release` stays out of neighbour
        reach. The writer could not drop another patch either, and nobody held an exit.
        """
        path = self.sealed_patch()
        self.writer.tool_pair_patch_verify(path)
        self.writer.tool_pair_patch_applied(path, ok=False, note="hunk rejected")
        self.ready()
        self.assertRefused(self.writer.tool_pair_advance, "done")
        result = self.writer.tool_pair_patch_release(path)
        self.assertEqual([], self.writer.unresolved_patches(self.store.read()))
        self.assertTrue(result["known_to_server"])
        self.assertEqual("done", self.writer.tool_pair_advance("done")["phase"])

    def test_a_writer_dropping_someone_elses_patch_is_marked_as_such(self) -> None:
        """Writer-dropped" and "author-dropped" differ, and the report must tell them apart."""
        path = self.sealed_patch()
        self.writer.tool_pair_patch_release(path)
        dropped = [e for e in self.store.read_transcript() if e.get("kind") == "patch_dropped"]
        self.assertTrue(dropped[0]["released_by_writer"])
        self.assertEqual("reviewer-agent", dropped[0]["owner"])

    def test_the_writer_cannot_drop_a_patch_already_applied(self) -> None:
        """An applied patch is no "unresolved". Deleting such a record hides both the apply
        fact and its author."""
        path = self.sealed_patch()
        self.writer.tool_pair_patch_verify(path)
        self.writer.tool_pair_patch_applied(path, ok=True)
        self.assertRefused(self.writer.tool_pair_patch_release, path, contains="already recorded")

    def test_a_read_only_role_still_cannot_drop_a_foreign_patch(self) -> None:
        """The writer right is not every right.

        The patch belongs to a third role: the writer may drop it, while another
        non-writer role may not. Or "the writer may" would turn into "all may" — exactly
        what the protocol fears.
        """
        third = PairSession("third-agent", self.store)
        path = third.tool_pair_patch_reserve()["path"]
        Path(path).write_text("--- a\n+++ b\n", encoding="utf-8")
        third.tool_pair_patch_seal(path)
        self.assertRefused(self.peer.tool_pair_patch_release, path, contains="belongs to")
        self.assertIn(path, [p["path"] for p in self.writer.unresolved_patches(self.store.read())])
        self.writer.tool_pair_patch_release(path)

    def test_release_works_when_the_file_was_already_deleted_by_hand(self) -> None:
        """`rm` past the server leaves a record in state.json, blocking the close. The record
        drops here, with no file needed."""
        path = self.sealed_patch()
        Path(path).unlink()
        result = self.writer.tool_pair_patch_release(path)
        self.assertFalse(result["deleted"])
        self.assertEqual([], self.writer.unresolved_patches(self.store.read()))

    def test_applied_requires_verify_first(self) -> None:
        path = self.sealed_patch()
        error = self.assertRefused(self.writer.tool_pair_patch_applied, path,
                                   contains="never verified")
        self.assertIn("pair_patch_verify", str(error.hint))

    def test_applied_requires_a_matching_hash(self) -> None:
        path = self.sealed_patch("original\n")
        Path(path).write_text("tampered\n", encoding="utf-8")
        self.writer.tool_pair_patch_verify(path)
        self.assertRefused(self.writer.tool_pair_patch_applied, path, contains="never verified")

    def test_proposer_may_not_record_the_apply(self) -> None:
        path = self.sealed_patch()
        self.assertRefused(self.peer.tool_pair_patch_applied, path, contains="only the writer")

    def test_audit_refuses_seal_and_verify_but_not_release(self) -> None:
        """In audit the whole pipeline closes, not only reserve: seal and verify refuse,
        release of garbage works. Or a "no diff" round would hand a green apply verdict
        (audit-leaves-patch-pipeline-open)."""

        path = self.peer.tool_pair_patch_reserve()["path"]
        Path(path).write_text("--- a\n+++ b\n", encoding="utf-8")
        self.store.mutate(lambda state: state.__setitem__("mode", "audit"))
        self.assertRefused(self.peer.tool_pair_patch_reserve, contains="audit")
        self.assertRefused(self.peer.tool_pair_patch_seal, path, contains="audit")
        self.assertRefused(self.writer.tool_pair_patch_verify, path, contains="audit")
        self.writer.tool_pair_patch_release(path)
        self.assertEqual(self.writer.unresolved_patches(self.store.read()), [])

    def test_audit_round_does_not_chase_patches(self) -> None:
        self.store.mutate(lambda state: state.__setitem__("mode", "audit"))
        self.assertEqual(self.writer.unresolved_patches(self.store.read()), [])


class TestTaskList(PairTestCase):
    """A task list instead of one line. Losing the middle is no model-memory question
    but a missing place for the task to live: state held one `task`, and a new task
    overwrote the old. Reproduced: the round closed as "task B" with evidence
    gathered for task A."""

    def add_task(self, text: str = "fix the panes", who: str = "human") -> int:
        self.store.mutate(lambda st: st.setdefault("tasks", []).append({
            "id": len(st.get("tasks") or []) + 1, "text": text, "status": "open",
            "added_at": "now", "added_by": who, "round": int(st.get("round", 0)),
            "reason": "", "closed_at": None, "closed_by": None}))
        return len(self.store.read()["tasks"])

    def setUp(self) -> None:
        super().setUp()
        self.writer.tool_pair_claim(reason="writer")

    def ready(self) -> None:
        self.advance_phases()
        self.human_ok()

    def test_a_new_task_does_not_erase_the_old_one(self) -> None:
        self.add_task("task A")
        self.add_task("task B")
        texts = [item["text"] for item in self.store.read()["tasks"]]
        self.assertEqual(["task A", "task B"], texts)

    def test_an_open_task_blocks_closing(self) -> None:
        ident = self.add_task("fix the panes")
        self.ready()
        error = self.assertRefused(self.writer.tool_pair_advance, "done")
        self.assertIn(f"task #{ident} is still open", str(error))
        self.assertIn("pair_task", str(error))

    def test_a_dropped_task_needs_a_reason(self) -> None:
        """Or the task vanishes silently, and exactly the failure the list exists for."""
        ident = self.add_task("turned out unneeded")
        self.assertRefused(self.writer.tool_pair_task, "drop", ident, contains="without a reason")
        self.assertEqual("open", self.store.read()["tasks"][0]["status"])

    def test_a_dropped_task_keeps_the_reason_on_the_record(self) -> None:
        ident = self.add_task("turned out unneeded")
        self.writer.tool_pair_task("drop", ident, reason="fixed in 8335dd2")
        item = self.store.read()["tasks"][0]
        self.assertEqual("dropped", item["status"])
        self.assertIn("8335dd2", item["reason"])
        self.assertEqual("writer-agent", item["closed_by"])

    def test_done_clears_the_stopper(self) -> None:
        ident = self.add_task("fix the panes")
        self.ready()
        self.writer.tool_pair_task("done", ident)
        self.record_test_evidence()
        self.assertEqual("done", self.writer.tool_pair_advance("done")["phase"])

    def test_the_critic_may_add_a_task_but_not_close_one(self) -> None:
        """Rights run asymmetric: the critic opens what it found, the writer implements
        and closes. Or the critic never adds, or it closes another one."""
        added = self.peer.tool_pair_task("add", text="another hole: pane_alive")
        self.assertEqual("open", added["task"]["status"])
        self.assertEqual("reviewer-agent", added["task"]["added_by"])
        self.assertRefused(self.peer.tool_pair_task, "done", added["task"]["id"],
                           contains="only the writer closes")

    def test_evidence_lands_on_the_only_open_task(self) -> None:
        ident = self.add_task("fix the panes")
        self.record_test_evidence()
        self.assertEqual(ident, self.store.read()["evidence"][-1]["task"])

    def test_evidence_with_several_open_tasks_is_not_guessed(self) -> None:
        """Never lie: never attribute a fact to a task it may miss, but never lose it either
        — the open-task snapshot rides the record."""
        self.add_task("task A")
        self.add_task("task B")
        self.record_test_evidence()
        entry = self.store.read()["evidence"][-1]
        self.assertIsNone(entry["task"])
        self.assertEqual([1, 2], entry["tasks_open"])

    def test_the_journal_shows_done_and_not_done(self) -> None:
        from server import render_tasks_md
        first = self.add_task("task A")
        second = self.add_task("task B")
        self.writer.tool_pair_claim(reason="writer")
        self.record_test_evidence(value="190 passed, exit 0", task=first)
        self.writer.tool_pair_task("drop", second, reason="covered by item 3")
        text = render_tasks_md(self.store.read())
        self.assertIn("#1 — NOT DONE", text)
        self.assertIn("#2 — dropped", text)
        self.assertIn("covered by item 3", text)
        self.assertIn("190 passed, exit 0", text)   # the evidence pins to #1


class TestClosingRuleIsSingleSourced(PairTestCase):
    """The round-close rule lives in one place — the server.

        The supervisor must show the human exactly what the server refuses. A second
        copy of the rule would drift from the first on the very first change, and the
        human would get "all good" from one side and refusal from the other."""

    def test_pilot_calls_the_servers_rule_not_its_own(self) -> None:
        import importlib.util
        pilot_path = Path(__file__).resolve().parent.parent.parent / "bin" / "pairpilot"
        module = importlib.util.module_from_spec(importlib.util.spec_from_loader("pilot_src", None))
        module.__dict__["__file__"] = str(pilot_path)
        exec(compile(pilot_path.read_text(encoding="utf-8"), "pairpilot", "exec"),
             module.__dict__)
        self.assertIs(module._done_blockers.__globals__["done_blockers"], done_blockers,
                      "the supervisor must call the server rule, not its copy")
