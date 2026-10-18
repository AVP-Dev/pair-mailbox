#!/usr/bin/env python3
"""Protocol tests: the transport — stdio round trips, heartbeat, audit trail."""

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

    def test_every_advertised_tool_is_dispatchable(self) -> None:
        """A tool declared in tools/list must stay callable.

        `pair_digest` was declared in `tool_definitions()` but missed in `HANDLERS`,
        and `invoke` answered it "unknown tool". Property: every declared tool resolves
        to a non-empty handler. The list comes from the server, never hand-listed — or
        the check would rot into repeating the same declaration.
        """
        from server import HANDLERS, tool_definitions

        advertised = {tool["name"] for tool in tool_definitions()}
        self.assertTrue(advertised, "the server declared zero tools")
        unhandled = sorted(
            name for name in advertised
            if HANDLERS.get(name) is None or not callable(HANDLERS.get(name))
        )
        self.assertEqual(unhandled, [], "declared but uncallable: " + ", ".join(unhandled))

    def test_pair_digest_answers_over_mcp(self) -> None:
        """The digest must work over MCP, not only via `pairctl digest`.

        The pair-work skill orders starting the round with `pair_digest` for token
        savings, and the `pair_state` cache refers to it too. An uncallable tool turns
        the skill instruction into a refusal, while the savings were promised to those
        who never get them.
        """
        process = self._start("writer-agent")
        try:
            self._rpc(process, {
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "clientInfo": {"name": "test"}},
            })
            self._notify(process, {"jsonrpc": "2.0", "method": "notifications/initialized"})
            called = self._rpc(process, {
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "pair_digest", "arguments": {"budget": 500}},
            })
            self.assertFalse(called["result"]["isError"], called["result"])
            payload = json.loads(called["result"]["content"][0]["text"])
            self.assertIn("digest", payload)
            self.assertIn("tokens", payload)
            # The digest must run cheaper than the full slice: or it gives no promised
            # savings and the pair-work skill lies.
            self.assertLess(payload["tokens"], DIGEST_TOKEN_BUDGET_FOR_TEST,
                            "the digest runs past its ceiling")
            self.assertIn("ROUND", payload["digest"])
        finally:
            process.stdin.close()
            process.wait(timeout=10)

    def test_notify_with_a_match_does_not_deadlock(self) -> None:
        """The critic deadlock seq 434: notify held the Lock calling send under the same
        Lock — the watch hung on the first notification, push dead. The RLock->Lock
        mutation must hang (proven by a 6s timeout live), this test is the guard."""
        import threading
        import server as _srv
        srv = _srv.MCPServer(self.writer)
        srv._subscriptions["s"] = "mailbox://writer-agent"
        done = threading.Event()
        def call() -> None:
            srv.notify("mailbox://writer-agent")
            done.set()
        worker = threading.Thread(target=call, daemon=True)
        worker.start()
        self.assertTrue(done.wait(timeout=5), "a matched notify must return, never hang")
        worker.join(timeout=5)

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



class TestHeartbeatIsEveryCall(PairTestCase):
    """Every call moves `last_seen` (heartbeat in invoke)."""

    def _backdate_writer(self) -> str:
        old = "2000-01-01T00:00:00+00:00"
        self.store.mutate(lambda state: state["roles"]["writer-agent"].__setitem__("last_seen", old))
        return old

    def test_note_moves_last_seen(self) -> None:
        self.writer.invoke("pair_state", {})
        old = self._backdate_writer()
        self.writer.invoke("pair_note", {"kind": "plan", "text": "working, not dropped"})
        fresh = self.store.read()["roles"]["writer-agent"]["last_seen"]
        self.assertNotEqual(old, fresh)
        self.assertGreater(fresh, old)

    def _close_round(self) -> None:
        self.writer.invoke("pair_claim", {})
        self.record_test_evidence()
        self.write_plan(int(self.store.read().get("round", 0)))
        self.ack_plan(int(self.store.read().get("round", 0)))
        for phase in ("plan", "implement", "verify", "done"):
            self.writer.invoke("pair_advance", {"phase": phase})

    def test_quiet_writer_in_done_is_abandoned(self) -> None:
        import server
        real_stall = server.STALL_SECONDS
        server.STALL_SECONDS = 3600
        try:
            self.writer.invoke("pair_state", {})
            self._close_round()
            self._backdate_writer()
            state = self.store.read()
            writer = state.get("writer")
            assert writer is not None
            self.assertTrue(self.writer._writer_is_abandoned(state, writer))
        finally:
            server.STALL_SECONDS = real_stall

    def test_active_writer_in_done_is_not_abandoned(self) -> None:
        import server
        real_stall = server.STALL_SECONDS
        server.STALL_SECONDS = 1
        try:
            self.writer.invoke("pair_state", {})
            self.peer.invoke("pair_state", {})
            self._close_round()
            # An active writer in done: notes move last_seen.
            for i in range(3):
                self.writer.invoke("pair_note", {"kind": "plan", "text": f"step {i}"})
            state = self.store.read()
            writer = state.get("writer")
            assert writer is not None
            self.assertFalse(self.writer._writer_is_abandoned(state, writer))
        finally:
            server.STALL_SECONDS = real_stall

    def test_refusal_is_also_a_heartbeat(self) -> None:
        self.writer.invoke("pair_state", {})
        self.writer.invoke("pair_claim", {})
        old = self._backdate_writer()
        # A refusal is a heartbeat too: the writer reserves no patches.
        self.assertRefused(self.writer.invoke, "pair_patch_reserve", {}, contains="must not")
        fresh = self.store.read()["roles"]["writer-agent"]["last_seen"]
        self.assertNotEqual(old, fresh)

    def test_ping_moves_last_seen_with_a_short_answer(self) -> None:
        """A ping is the same heartbeat at a three-field price, not 939 tokens.

        The pilot-heartbeat-not-just-silence card: an agent writing code for 20
        minutes with no calls reads dead to the pilot, alive to the cause. A ping goes
        out alone every N minutes; the pilot tells "silent, yet pinging" apart.
        """
        self.writer.invoke("pair_state", {})
        old = self._backdate_writer()
        answer = self.writer.invoke("pair_ping", {})
        fresh = self.store.read()["roles"]["writer-agent"]["last_seen"]
        self.assertNotEqual(old, fresh)
        self.assertGreater(fresh, old)
        self.assertEqual(set(answer), {"role", "last_seen", "pid"})
        self.assertEqual(answer["role"], "writer-agent")



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
        reopened = Store(self.root, allow_create=True)
        self.assertGreaterEqual(len(reopened.read_transcript()), before)
