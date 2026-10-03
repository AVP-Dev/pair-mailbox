#!/usr/bin/env python3
"""Protocol tests for pair-mailbox.

Run: python3 -m unittest discover -s mcp/pair_mailbox -v
  or: python3 mcp/pair_mailbox/test_protocol.py

The interesting tests are the refusals. This server's value is that it says no:
a second writer, a peer that tries to move authority, a patch that changed after
it was sealed, a glob where an exact path belongs.
"""

from __future__ import annotations

import contextlib
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

sys.path.insert(0, str(Path(__file__).resolve().parent))

from server import (  # noqa: E402
    MAX_READ_WAIT_SECONDS,
    PairSession,
    ProtocolError,
    Store,
    discover_projects,
    main,
    project_row,
    render_table,
    silence_seconds,
)

SERVER = Path(__file__).resolve().parent / "server.py"
PAIRCHAT = Path(__file__).resolve().parent.parent.parent / "bin" / "pairchat"


class PairTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "state"
        self.store = Store(self.root)
        self.writer = PairSession("writer-agent", self.store)
        self.peer = PairSession("reviewer-agent", self.store)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def assertRefused(self, callable_, *args, contains: str = "", **kwargs):
        with self.assertRaises(ProtocolError) as caught:
            callable_(*args, **kwargs)
        if contains:
            self.assertIn(contains, str(caught.exception).lower())
        return caught.exception


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


class TestMessaging(PairTestCase):
    def test_label_is_applied_by_the_server(self) -> None:
        self.writer.tool_pair_state()
        self.peer.tool_pair_state()

        self.writer.tool_pair_send(to="reviewer-agent", text="here is my read on the bug")
        messages = self.peer.tool_pair_read()
        self.assertEqual(messages["count"], 1)

        message = messages["messages"][0]
        self.assertEqual(message["from"], "writer-agent")
        self.assertEqual(message["label"], "Chat from writer-agent: ")
        self.assertIn("here is my read", message["text"])

    def test_read_marks_messages_read(self) -> None:
        self.writer.tool_pair_state()
        self.peer.tool_pair_state()
        self.writer.tool_pair_send(to="reviewer-agent", text="one")
        self.writer.tool_pair_send(to="reviewer-agent", text="two")

        first = self.peer.tool_pair_read()
        self.assertEqual(first["count"], 2)
        second = self.peer.tool_pair_read()
        self.assertEqual(second["count"], 0)

    def test_cannot_send_to_yourself_or_nowhere(self) -> None:
        self.writer.tool_pair_state()
        self.assertRefused(self.writer.tool_pair_send, to="writer-agent", text="me", contains="own role")
        self.assertRefused(self.writer.tool_pair_send, to="", text="me", contains="required")
        self.assertRefused(self.writer.tool_pair_send, to="peer", text="  ", contains="required")

    def test_writer_may_wait_too(self) -> None:
        """Both roles may wait. This used to be forced to zero for the writer, to

        stop two agents blocking each other, but that killed the channel: an agent that
        cannot wait falls asleep and never notices an incoming message, so the human
        starts relaying by hand. A bounded wait cannot deadlock, because it expires and
        hands the caller back to work.
        """
        self.writer.tool_pair_state()
        self.peer.tool_pair_state()
        self.writer.tool_pair_claim()

        started = time.monotonic()
        result = self.writer.tool_pair_read(wait_seconds=1)
        elapsed = time.monotonic() - started

        self.assertGreater(result["waited_seconds"], 0.0, "the writer must be able to wait")
        self.assertLess(elapsed, 4.0, "but the wait stays bounded")

    def test_wait_is_capped(self) -> None:
        """The cap must be asserted without actually sitting through it.

        Asking for more than the cap does not help: the clamp happens before the wait,
        so the suite would still spend the full minute. Lower the cap instead, then ask
        for more than it. The constant is read at call time, so patching the module
        attribute is enough.
        """
        import server

        self.writer.tool_pair_state()
        original = server.MAX_READ_WAIT_SECONDS
        server.MAX_READ_WAIT_SECONDS = 1
        try:
            result = self.writer.tool_pair_read(wait_seconds=9999)
            self.assertLessEqual(result["waited_seconds"], 1)
        finally:
            server.MAX_READ_WAIT_SECONDS = original

    def test_peer_wait_is_bounded(self) -> None:
        self.peer.tool_pair_state()
        started = time.monotonic()
        result = self.peer.tool_pair_read(wait_seconds=2)
        elapsed = time.monotonic() - started
        self.assertEqual(result["count"], 0)
        self.assertLess(elapsed, 5.0)

    def test_negative_wait_is_refused(self) -> None:
        self.assertRefused(self.peer.tool_pair_read, wait_seconds=-1, contains="negative")


class TestPhasesAndNotes(PairTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.writer.tool_pair_state()
        self.peer.tool_pair_state()
        self.writer.tool_pair_claim()

    def test_legal_and_illegal_transitions(self) -> None:
        self.writer.tool_pair_advance("plan")
        self.assertEqual(self.store.read()["phase"], "plan")
        self.assertRefused(self.writer.tool_pair_advance, "done", contains="cannot move")
        self.writer.tool_pair_advance("implement")
        self.writer.tool_pair_advance("verify")
        self.writer.tool_pair_advance("done")

    def test_non_writer_cannot_advance_phase(self) -> None:
        self.assertRefused(self.peer.tool_pair_advance, "plan", contains="only the writer")

    def test_unknown_phase_is_refused(self) -> None:
        self.assertRefused(self.writer.tool_pair_advance, "shipping", contains="unknown phase")

    def test_notes_accumulate_per_kind(self) -> None:
        self.writer.tool_pair_note(kind="plan", text="step one: read the config loader")
        self.peer.tool_pair_note(kind="finding", text="config loader swallows the empty case")
        self.writer.tool_pair_note(kind="plan", text="step two: add the regression test")

        plan = (self.store.notes_root / "plan.md").read_text(encoding="utf-8")
        self.assertIn("step one", plan)
        self.assertIn("step two", plan)
        self.assertIn("config loader swallows", (self.store.notes_root / "finding.md").read_text(encoding="utf-8"))

    def test_note_kind_must_be_a_slug(self) -> None:
        self.assertRefused(self.writer.tool_pair_note, kind="../escape", text="x", contains="slug")
        self.assertRefused(self.writer.tool_pair_note, kind="plan", text="", contains="required")


class TestAuditTrail(PairTestCase):
    def test_every_action_is_recorded(self) -> None:
        self.writer.tool_pair_state()
        self.peer.tool_pair_state()
        self.writer.tool_pair_claim()
        self.writer.tool_pair_send(to="reviewer-agent", text="proposal")
        path = self.peer.tool_pair_patch_reserve()["path"]
        Path(path).write_text("diff\n", encoding="utf-8")
        self.peer.tool_pair_patch_seal(path)
        self.writer.tool_pair_patch_verify(path)
        self.peer.tool_pair_patch_release(path)

        # Refusals belong in the trail too, not just successes. The peer probing
        # for authority is precisely the event worth keeping.
        self.assertRefused(self.peer.invoke, "pair_claim", {})
        self.assertRefused(self.writer.invoke, "pair_patch_reserve", {})

        kinds = [entry["kind"] for entry in self.store.read_transcript()]
        for expected in ("claim", "send", "patch_reserve", "patch_seal", "patch_verify", "patch_release", "refusal"):
            self.assertIn(expected, kinds)

        refusals = [e for e in self.store.read_transcript() if e["kind"] == "refusal"]
        by_tool = {entry["tool"]: entry for entry in refusals}
        self.assertIn("pair_claim", by_tool)
        self.assertEqual(by_tool["pair_claim"]["role"], "reviewer-agent")
        self.assertIn("held by", by_tool["pair_claim"]["reason"])
        self.assertIn("pair_patch_reserve", by_tool)
        self.assertEqual(by_tool["pair_patch_reserve"]["role"], "writer-agent")

    def test_invoke_rejects_an_unknown_tool(self) -> None:
        self.assertRefused(self.writer.invoke, "pair_nope", {}, contains="unknown tool")

    def test_transcript_is_append_only_across_sessions(self) -> None:
        self.writer.tool_pair_state()
        self.writer.tool_pair_claim()
        before = len(self.store.read_transcript())
        reopened = Store(self.root)
        self.assertGreaterEqual(len(reopened.read_transcript()), before)


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


class TestMCPStdio(PairTestCase):
    """End-to-end over the wire, the way a real MCP client talks to it."""

    def _rpc(self, process, payload: dict) -> dict:
        process.stdin.write(json.dumps(payload) + "\n")
        process.stdin.flush()
        line = process.stdout.readline()
        if not line:
            raise AssertionError("server closed stdout")
        return json.loads(line)

    def _notify(self, process, payload: dict) -> None:
        """Notifications carry no id and must not be answered."""
        process.stdin.write(json.dumps(payload) + "\n")
        process.stdin.flush()

    def _start(self, role: str) -> subprocess.Popen:
        return subprocess.Popen(
            [sys.executable, str(SERVER), "--role", role, "--state", str(self.root)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )

    def test_handshake_list_and_call(self) -> None:
        process = self._start("writer-agent")
        try:
            init = self._rpc(process, {
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "clientInfo": {"name": "test"}},
            })
            self.assertEqual(init["result"]["protocolVersion"], "2025-06-18")
            self.assertIn("tools", init["result"]["capabilities"])
            self.assertIn("writer-agent", init["result"]["instructions"])

            self._notify(process, {"jsonrpc": "2.0", "method": "notifications/initialized"})
            listed = self._rpc(process, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            names = {tool["name"] for tool in listed["result"]["tools"]}
            # Derived from the server rather than hardcoded, so adding a tool does
            # not break a test that has nothing to do with tool count.
            from server import tool_definitions

            self.assertEqual(names, {t["name"] for t in tool_definitions()})
            self.assertIn("pair_claim", names)
            self.assertIn("pair_patch_verify", names)

            claimed = self._rpc(process, {
                "jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "pair_claim", "arguments": {}},
            })
            self.assertFalse(claimed["result"]["isError"])
            payload = json.loads(claimed["result"]["content"][0]["text"])
            self.assertEqual(payload["writer"], "writer-agent")

            refused = self._rpc(process, {
                "jsonrpc": "2.0", "id": 4, "method": "tools/call",
                "params": {"name": "pair_patch_reserve", "arguments": {}},
            })
            self.assertTrue(refused["result"]["isError"])
            self.assertIn("write authority", refused["result"]["content"][0]["text"])

            unknown = self._rpc(process, {
                "jsonrpc": "2.0", "id": 5, "method": "tools/call",
                "params": {"name": "pair_nope", "arguments": {}},
            })
            self.assertTrue(unknown["result"]["isError"])
        finally:
            process.stdin.close()
            process.wait(timeout=10)

    def test_malformed_input_gets_a_jsonrpc_error_not_a_crash(self) -> None:
        process = self._start("writer-agent")
        try:
            process.stdin.write("this is not json\n")
            process.stdin.flush()
            self.assertEqual(json.loads(process.stdout.readline())["error"]["code"], -32700)

            bad = self._rpc(process, {"jsonrpc": "2.0", "id": 9, "method": "no/such/method"})
            self.assertEqual(bad["error"]["code"], -32601)

            alive = self._rpc(process, {
                "jsonrpc": "2.0", "id": 10, "method": "tools/call",
                "params": {"name": "pair_state", "arguments": {}},
            })
            self.assertIn("result", alive)
        finally:
            process.stdin.close()
            process.wait(timeout=10)

    def test_two_processes_share_state(self) -> None:
        writer = self._start("writer-agent")
        reviewer = self._start("reviewer-agent")
        try:
            self._rpc(writer, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                               "params": {"protocolVersion": "2024-11-05"}})
            self._notify(writer, {"jsonrpc": "2.0", "method": "notifications/initialized"})
            self._rpc(reviewer, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                 "params": {"protocolVersion": "2024-11-05"}})
            self._notify(reviewer, {"jsonrpc": "2.0", "method": "notifications/initialized"})

            self._rpc(writer, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                               "params": {"name": "pair_claim", "arguments": {}}})
            blocked = self._rpc(reviewer, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                          "params": {"name": "pair_claim", "arguments": {}}})
            self.assertTrue(blocked["result"]["isError"])
            self.assertIn("held by", blocked["result"]["content"][0]["text"])

            self._rpc(writer, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                               "params": {"name": "pair_send",
                                          "arguments": {"to": "reviewer-agent", "text": "read the diff"}}})
            got = self._rpc(reviewer, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                       "params": {"name": "pair_read", "arguments": {}}})
            messages = json.loads(got["result"]["content"][0]["text"])["messages"]
            self.assertEqual(messages[0]["text"], "read the diff")
            self.assertEqual(messages[0]["label"], "Chat from writer-agent: ")
        finally:
            for process in (writer, reviewer):
                process.stdin.close()
                process.wait(timeout=10)


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


class TestMultiProject(PairTestCase):
    """Видеть все проекты сразу, а не переключаться между окнами.

    Здесь же регрессия на `pairchat --once`: флага не было, а я всё равно советовал
    его набирать. Тест запускает скрипт как подпроцесс и требует, чтобы он сам
    завершился — иначе это снова ложь в документации.
    """

    def _project(self, name: str, *, phase: str = "scoping", writer=None, events: int = 0):
        root = Path(self._tmp.name) / name / ".pair"
        store = Store(root)
        store.mutate(lambda state: state.update({"phase": phase, "writer": writer,
                                                 "task": f"задача {name}"}))
        for _ in range(events):
            store.append_transcript("claim", writer or "opencode", writer=writer or "opencode")
        return root

    def setUp(self) -> None:
        super().setUp()
        # discover_projects по замыслу не теряет проект, в котором стоишь, — а мы стоим
        # в репозитории, где .pair есть. Без chdir каждый тест видел бы ещё и umputum.
        self._cwd = os.getcwd()
        os.chdir(self._tmp.name)

    def tearDown(self) -> None:
        os.chdir(self._cwd)
        os.environ.pop("PAIR_SCAN", None)
        super().tearDown()

    def test_discovers_only_directories_that_actually_have_state(self) -> None:
        good = self._project("good", events=1)
        # каталог без state.json — не проект, и в таблице у него быть не должно
        (Path(self._tmp.name) / "empty" / ".pair").mkdir(parents=True)
        os.environ["PAIR_SCAN"] = str(Path(self._tmp.name) / "*" / ".pair")
        try:
            found = discover_projects()
        finally:
            del os.environ["PAIR_SCAN"]
        self.assertIn(good.resolve(), found)
        self.assertNotIn((Path(self._tmp.name) / "empty" / ".pair").resolve(), found)

    def test_pair_scan_replaces_the_default_globs(self) -> None:
        inside = self._project("inside", events=1)
        os.environ["PAIR_SCAN"] = str(Path(self._tmp.name) / "inside" / ".pair")
        try:
            found = discover_projects()
        finally:
            del os.environ["PAIR_SCAN"]
        self.assertEqual(found, [inside.resolve()])

    def test_accepts_a_project_directory_not_only_the_pair_directory(self) -> None:
        self._project("proj", events=1)
        found = discover_projects([str(Path(self._tmp.name) / "proj")])
        self.assertEqual(found, [(Path(self._tmp.name) / "proj" / ".pair").resolve()])

    def test_same_project_given_twice_appears_once(self) -> None:
        root = self._project("twice", events=1)
        found = discover_projects([str(root), str(root), str(root)])
        self.assertEqual(found, [root.resolve()])

    def test_cwd_project_survives_an_empty_pair_scan(self) -> None:
        # где бы человек ни стоял, его собственный проект не должен пропасть из вида
        root = self._project("here", events=1)
        cwd = os.getcwd()
        os.chdir(root.parent)
        os.environ["PAIR_SCAN"] = str(Path(self._tmp.name) / "nothing-matches")
        try:
            found = discover_projects()
        finally:
            del os.environ["PAIR_SCAN"]
            os.chdir(cwd)
        self.assertIn(root.resolve(), found)

    def test_silence_is_none_when_nothing_was_ever_recorded(self) -> None:
        self.assertIsNone(silence_seconds(self._project("quiet")))

    def test_silence_counts_from_the_last_event(self) -> None:
        root = self._project("noisy", events=3)
        age = silence_seconds(root)
        self.assertIsNotNone(age)
        self.assertGreaterEqual(age, 0.0)
        self.assertLess(age, 60.0)

    def test_silence_ignores_state_writes_that_changed_nothing(self) -> None:
        # пишем state.json без единого события: пара не двигалась, значит и молчание
        # не должно выглядеть как свежая активность
        root = self._project("idle", events=1)
        Store(root).mutate(lambda state: state.update({"task": "трогаем state"}))
        self.assertLess(silence_seconds(root), 60.0)

    def test_a_finished_project_is_never_reported_as_stuck(self) -> None:
        root = self._project("finished", phase="done", writer="opencode", events=1)
        row = project_row(root, stuck_min=0)
        self.assertTrue(row["closed"])
        self.assertFalse(row["stuck"], "a done project is not stalled, however long it has been quiet")

    def test_an_unfinished_project_that_went_quiet_is_stuck(self) -> None:
        root = self._project("stalled", phase="verify", writer="opencode", events=1)
        row = project_row(root, stuck_min=0)
        self.assertFalse(row["closed"])
        self.assertTrue(row["stuck"])

    def test_table_columns_line_up_for_long_project_names(self) -> None:
        self._project("a-very-long-project-name-here", events=1)
        self._project("tiny", events=1)
        os.environ["PAIR_SCAN"] = str(Path(self._tmp.name) / "*" / ".pair")
        try:
            rows = [project_row(root) for root in discover_projects()]
        finally:
            del os.environ["PAIR_SCAN"]
        lines = render_table(rows).splitlines()
        self.assertGreaterEqual(len(lines), 4)

        def columns(line: str, count: int = 5) -> list:
            # последний столбец может содержать пробелы, поэтому сравниваем только
            # начала первых пяти: именно они должны совпадать на всех строках
            return [match.start() for match in re.finditer(r"\S+", line)][:count]

        reference = columns(lines[0])
        self.assertEqual(len(reference), 5)
        for line in lines[2:]:
            if line.strip():
                self.assertEqual(columns(line), reference,
                                 "столбцы разъехались:\n" + "\n".join(lines))

    def test_ctl_all_gives_one_row_per_project(self) -> None:
        self._project("alpha", events=1)
        self._project("beta", phase="done", writer="agy", events=1)
        os.environ["PAIR_SCAN"] = str(Path(self._tmp.name) / "*" / ".pair")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["all"])
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertIn("alpha", text)
        self.assertIn("beta", text)
        # ровно наши два проекта, ничего лишнего из глобов по умолчанию
        table = text.split("\n\n")[0].splitlines()
        names = [line.split()[0] for line in table[2:] if line.strip()]
        self.assertEqual(sorted(names), ["alpha", "beta"])

    def test_ctl_all_json_is_machine_readable(self) -> None:
        self._project("gamma", events=1)
        os.environ["PAIR_SCAN"] = str(Path(self._tmp.name) / "*" / ".pair")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            main(["all", "--json"])
        rows = json.loads(out.getvalue())
        self.assertEqual([row["project"] for row in rows], ["gamma"])
        for row in rows:
            for key in ("project", "phase", "writer", "silence", "stuck", "closed", "path"):
                self.assertIn(key, row)

    def test_ctl_all_signals_stall_with_a_distinct_exit_code(self) -> None:
        # 3 — чтобы скрипт мог отличить «всё живо» от «кто-то встал»
        self._project("dead", phase="verify", writer="opencode", events=1)
        os.environ["PAIR_SCAN"] = str(Path(self._tmp.name) / "*" / ".pair")
        try:
            code = main(["all", "--stuck-min", "0"])
        finally:
            del os.environ["PAIR_SCAN"]
        self.assertEqual(code, 3)

    def test_ctl_all_says_where_to_look_when_there_is_nothing(self) -> None:
        os.environ["PAIR_SCAN"] = str(Path(self._tmp.name) / "nothing-matches")
        cwd = os.getcwd()
        os.chdir(self._tmp.name)
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = main(["all"])
        finally:
            del os.environ["PAIR_SCAN"]
            os.chdir(cwd)
        self.assertEqual(code, 1)
        self.assertIn("PAIR_SCAN", out.getvalue())

    def test_pairchat_once_exits_on_its_own(self) -> None:
        # ровно то, в чём я ошибался: флага не было, скрипт висел до Ctrl-C
        self.store.mutate(lambda state: state.update({"phase": "done", "writer": "writer-agent"}))
        self.store.append_transcript("claim", "writer-agent", writer="writer-agent")
        (self.root / "notes").mkdir(parents=True, exist_ok=True)
        (self.root / "notes" / "plan.md").write_text("план на две строки\n", encoding="utf-8")
        env = dict(os.environ, PAIR_STATE=str(self.root))
        try:
            done = subprocess.run([sys.executable, str(PAIRCHAT), "--once"],
                                  capture_output=True, text=True, timeout=30, env=env)
        except subprocess.TimeoutExpired:
            self.fail("--once не завершился сам: скрипт висит, как и до флага")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("ПИСАТЕЛЬ: writer-agent", done.stdout)
        self.assertIn("план на две строки", done.stdout)

    def test_naming_a_project_replaces_the_built_in_search(self) -> None:
        # иначе «посмотри ~/work/blog» показал бы пять проектов из глобов плюс blog
        named = self._project("named", events=1)
        self.assertEqual(discover_projects([str(named)]), [named.resolve()])

    def test_pairchat_all_prints_a_header_and_labels_every_project(self) -> None:
        for name in ("proj-one", "proj-two"):
            self._project(name, events=1)
        done = subprocess.run(
            [sys.executable, str(PAIRCHAT), "--all", "--once"],
            capture_output=True, text=True, timeout=30,
            env=dict(os.environ, PAIR_SCAN=str(Path(self._tmp.name) / "*" / ".pair")),
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        plain = re.sub(r"\x1b\[[0-9;]*m", "", done.stdout)
        self.assertIn("proj-one", plain)
        self.assertIn("proj-two", plain)
        self.assertIn("фаза", plain.splitlines()[0])
        for line in plain.splitlines():
            if "CLAIM" in line or "ADVANCE" in line:
                head = line.split("CLAIM")[0].split("ADVANCE")[0].strip()
                self.assertRegex(head, r"^(proj-one|proj-two)\s+\[\d\d:\d\d:\d\d\]$",
                                 "у блока нет метки проекта: " + line)

    def test_pairchat_shows_only_the_project_you_are_in(self) -> None:
        # Пользователь попросил именно этого: в папке проекта — диалог этого
        # проекта, без чужих. Смешанная выдача здесь вредна.
        mine = self._project("mine", events=1)
        self._project("theirs", events=1)
        os.environ["PAIR_SCAN"] = str(Path(self._tmp.name) / "*" / ".pair")
        cwd = os.getcwd()
        os.chdir(mine.parent)
        try:
            done = subprocess.run([sys.executable, str(PAIRCHAT), "--once"],
                                  capture_output=True, text=True, timeout=30)
        finally:
            del os.environ["PAIR_SCAN"]
            os.chdir(cwd)
        self.assertEqual(done.returncode, 0, done.stderr)
        plain = re.sub(r"\x1b\[[0-9;]*m", "", done.stdout)
        self.assertIn("mine", plain)
        self.assertNotIn("theirs", plain, "показан чужой проект без --all")

    def test_pairchat_outside_a_project_points_back_at_the_project(self) -> None:
        self._project("somewhere-else", events=1)
        cwd = os.getcwd()
        os.chdir(self._tmp.name)  # тут .pair нет
        try:
            done = subprocess.run([sys.executable, str(PAIRCHAT), "--once"],
                                  capture_output=True, text=True, timeout=30)
        finally:
            os.chdir(cwd)
        self.assertEqual(done.returncode, 1)
        text = done.stdout + done.stderr
        self.assertIn("--all", text, "не сказано, что делать, чтобы увидеть всё")
        self.assertIn("cd", text, "не сказано, что надо зайти в папку проекта")

    def test_agent_notes_are_shown_by_default_and_work_notes_are_not(self) -> None:
        # Заметки агентов (plan, finding...) — часть работы, их видно.
        # Мои рабочие заметки о самом pair-mailbox — нет, их сотни строк кода.
        for kind in ("plan", "finding", "verdict", "z-scratch"):
            path = self.root / "notes" / f"{kind}.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"содержимое заметки {kind}\n", encoding="utf-8")
        self.store.mutate(lambda state: state.update({"phase": "plan"}))
        self.store.append_transcript("note", "writer-agent", note_kind="plan", chars=10)
        env = dict(os.environ, PAIR_STATE=str(self.root))
        cwd = os.getcwd()
        os.chdir(self.root.parent)
        try:
            done = subprocess.run([sys.executable, str(PAIRCHAT), "--once"],
                                  capture_output=True, text=True, timeout=30, env=env)
            loud = subprocess.run([sys.executable, str(PAIRCHAT), "--all-notes", "--once"],
                                  capture_output=True, text=True, timeout=30, env=env)
        finally:
            os.chdir(cwd)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("содержимое заметки plan", done.stdout)
        self.assertIn("содержимое заметки verdict", done.stdout)
        self.assertNotIn("z-scratch", done.stdout, "рабочая заметка попала в выдачу")
        self.assertIn("содержимое заметки z-scratch", loud.stdout,
                      "--all-notes должен показывать и рабочие заметки")

    def test_long_lines_are_wrapped_to_a_readable_width(self) -> None:
        # Агенты пишут абзацами по 300+ символов; без переноса терминал рвёт
        # их посреди слова.
        self.store.mutate(lambda state: state.update({"phase": "plan"}))
        (self.root / "mailbox" / "peer").mkdir(parents=True, exist_ok=True)
        payload = " ".join(["слово%02d" % i for i in range(60)])
        (self.root / "mailbox" / "peer" / "000001-t.json").write_text(
            json.dumps({"seq": 1, "ts": "2026-10-02T20:00:00+00:00",
                        "from": "peer", "to": "writer-agent", "subject": "длинная тема " + payload,
                        "text": payload, "label": "Chat from peer: "}),
            encoding="utf-8")
        env = dict(os.environ, PAIR_STATE=str(self.root))
        done = subprocess.run([sys.executable, str(PAIRCHAT), "--once"],
                              capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual(done.returncode, 0, done.stderr)
        plain = re.sub(r"\x1b\[[0-9;]*m", "", done.stdout)
        for line in plain.splitlines():
            self.assertLessEqual(len(line), 105,
                                 "строка не перенесена: " + repr(line[:120]))

    def test_refuses_when_there_is_no_state(self) -> None:
        empty = Path(self._tmp.name) / "no-project"
        empty.mkdir()
        done = subprocess.run(
            [sys.executable, str(PAIRCHAT), "--once"],
            capture_output=True, text=True, timeout=30,
            env=dict(os.environ, PAIR_STATE=str(empty / ".pair")),
        )
        self.assertEqual(done.returncode, 1)
        self.assertIn("нет", done.stdout + done.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)