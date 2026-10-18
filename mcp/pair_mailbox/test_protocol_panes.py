#!/usr/bin/env python3
"""Protocol tests: pane identity is not liveness — stale panes, single live process per role."""

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


class TestStalePaneSurvives(PairTestCase):
    """The roster must never drop a live role pane.

    Such cost us a night: the pane lived, the pid belonged to a role that merely
    restarted, and registering another role decided "the pid is foreign and dead" and
    dropped `pane`. Delivery to the live neighbour then refused with "no tmux pane",
    while the neighbour sat in the next window waiting.
    """

    def setUp(self) -> None:
        super().setUp()
        import server
        self.server = server

    def pane_alive(self, value: bool):
        """Swap pane_exists for the test run and always return it.

        The stub plants here, not in setUp, because two tests below check pane_exists
        itself — with a stable stub it would answer right for the wrong reason.
        Both pane questions check here: is the pane alive AND is it ours. One stub
        never suffices — the second check would reach the real tmux, and the "dead
        pane" test would pass for the wrong reason.
        """
        import composer

        real = self.server.pane_exists
        real_foreign = composer.pane_is_foreign
        real_belongs = composer.pane_belongs_to_project
        self.server.pane_exists = lambda pane: value
        if value:
            composer.pane_is_foreign = lambda tmux, pane, root: (False, "")
            composer.pane_belongs_to_project = lambda tmux, pane, root: (True, "")
        else:
            composer.pane_is_foreign = lambda tmux, pane, root: (True, "pane missing")
            # A dead pane belongs to the project — merely dead. Confusing these two facts
            # is banned, or the "ours" check would answer "no" and drop the roster record by
            # the wrong path checked here.
            composer.pane_belongs_to_project = lambda tmux, pane, root: (True, "")
        self.addCleanup(setattr, self.server, "pane_exists", real)
        self.addCleanup(setattr, composer, "pane_is_foreign", real_foreign)
        self.addCleanup(setattr, composer, "pane_belongs_to_project", real_belongs)

    def register(self, role: str, pane: str, pid: int) -> PairSession:
        self.store.mutate(lambda s: s.setdefault("roles", {}).setdefault(role, {}).update(
            {"pane": pane, "pid": pid}))
        return PairSession(role, self.store)

    def join_pane(self, pane: str) -> None:
        """A new role enters a pane, and _register starts checking it.

        A mandatory test step, not decor: the pane conflict only surfaces where the
        REGISTERING role owns a pane.
        """
        previous = os.environ.get("TMUX_PANE")
        os.environ["TMUX_PANE"] = pane
        self.addCleanup(lambda: os.environ.__setitem__("TMUX_PANE", previous)
                        if previous is not None else os.environ.pop("TMUX_PANE", None))

    def test_live_pane_of_a_restarted_role_is_kept(self) -> None:
        """A role restarted: the pid changed, but the window stays. The pane must hold, or
        the neighbour turns unreachable exactly when needed."""
        self.pane_alive(True)                        # the window lives, the pid does not
        self.register("agy", "%20", 999999)
        newcomer = PairSession("claude", self.store)
        newcomer.tool_pair_state()
        self.assertEqual(self.store.read()["roles"]["agy"].get("pane"), "%20")

    def test_pane_is_cleared_when_both_facts_hold(self) -> None:
        """A dead pid AND no pane in tmux means the pane is truly dead."""
        self.pane_alive(False)
        self.register("agy", "%20", 999999)
        self.join_pane("%20")
        newcomer = PairSession("claude", self.store)
        newcomer.tool_pair_state()
        roles = self.store.read()["roles"]
        self.assertIsNone(roles["agy"].get("pane"))
        kinds = [e["kind"] for e in self.store.read_transcript()]
        self.assertIn("pane_stale_cleared", kinds)

    def test_live_pid_on_the_same_pane_is_still_a_collision(self) -> None:
        """Two live roles on one pane means indistinguishable, and that must be said, never
        silently picking one."""
        self.pane_alive(True)
        self.register("agy", "%20", os.getpid())
        self.join_pane("%20")
        newcomer = PairSession("claude", self.store)
        newcomer.tool_pair_state()
        kinds = [e["kind"] for e in self.store.read_transcript()]
        self.assertIn("pane_collision", kinds)
        self.assertEqual(self.store.read()["roles"]["agy"].get("pane"), "%20")

    def test_two_roles_on_one_unique_pane_id_is_a_collision(self) -> None:
        """Two roles on one unique pane ID means a UID collision (#guard).

        The `ml:0.2` address floats on splits, `%N` on tmux restarts; `%2751` is unique
        and dies with the pane. Two roles sharing a UID means the same indistinction
        as sharing addresses, never silent: or delivery lands in a foreign composer.
        """
        self.pane_alive(True)
        self.register("agy", "%20", os.getpid())
        self.store.mutate(lambda s: s["roles"]["agy"].update({"pane_uid": "%999"}))
        self.join_pane("%21")
        import composer
        real_ident = composer.pane_identity
        composer.pane_identity = lambda tmux, pane: {
            "exists": True, "alive": True, "path": "/x", "address": "ml:0.1",
            "session_id": "$1", "pane_uid": "%999"}
        self.addCleanup(setattr, composer, "pane_identity", real_ident)
        newcomer = PairSession("claude", self.store)
        newcomer.tool_pair_state()
        kinds = [e["kind"] for e in self.store.read_transcript()]
        self.assertIn("pane_uid_collision", kinds)

    def test_daemon_foreign_env_explicit_channel_wins(self) -> None:
        """A daemon with foreign TMUX_PANE takes its address from the explicit channel
        (--pane/PAIR_PANE).

        The OpenCode daemon inherits another project TMUX_PANE (say gala:0.1). The
        explicit channel from the launching side wins over env and ancestry.
        """
        import composer
        self.pane_alive(True)
        self.join_pane("%foreign_pane")
        real_belongs = composer.pane_belongs_to_project
        composer.pane_belongs_to_project = lambda tmux, pane, root: (
            (True, "") if str(pane) in ("%explicit_pane", "%explicit_env", "proj:0.1") else (False, "another project")
        )
        self.addCleanup(setattr, composer, "pane_belongs_to_project", real_belongs)
        newcomer = PairSession("claude", self.store, pane="%explicit_pane")
        newcomer.tool_pair_state()
        roles = self.store.read()["roles"]
        self.assertEqual(roles["claude"].get("pane"), "%explicit_pane")

    def test_deadlock_living_host_takes_over_dead_occupant_pane(self) -> None:
        """Deadlock: a live owner takes a dead holder pane.

        The pane lives and is project, the holder is dead (pid 999999). The incoming
        live role with the same pane drops the old record with pane_seen.
        """
        self.pane_alive(True)
        self.register("agy", "%20", 999999)
        self.join_pane("%20")
        newcomer = PairSession("claude", self.store, pane="%20")
        newcomer.tool_pair_state()
        roles = self.store.read()["roles"]
        self.assertIsNone(roles["agy"].get("pane"))
        self.assertEqual(roles["claude"].get("pane"), "%20")
        kinds = [e["kind"] for e in self.store.read_transcript()]
        self.assertIn("pane_stale_cleared", kinds)

    def test_a_malformed_id_is_not_vouched_for(self) -> None:
        """Past a reboot %1 may belong to a wholly other window, so the pane checks only by
        a real tmux query, never by line shape."""
        self.assertFalse(self.server.pane_exists(""))
        self.assertFalse(self.server.pane_exists("%999999999"), "a missing pane is not alive")
        self.assertFalse(self.server.pane_exists("opencode"), "a process name is not a pane")

    def test_a_live_pane_is_recognised(self) -> None:
        if not shutil_which("tmux"):
            self.skipTest("tmux not installed")
        mine = self._pane_at_startup
        if not mine:
            self.skipTest("the suite runs outside tmux")
        self.assertTrue(self.server.pane_exists(mine))

    def test_the_two_pane_checks_never_disagree(self) -> None:
        """server.pane_exists and composer.pane_alive do the same thing.

        The phantom-pane fix first landed in one function, while the second with the
        same name and job went missing: `pairpilot` calls exactly it, and `%999999`
        stayed "alive" forever. Easiest never to let diverge is comparing — a fix to
        one function without the other drops this test.
        """
        from composer import pane_alive
        if not shutil_which("tmux"):
            self.skipTest("tmux not installed")
        tmux = shutil_which("tmux")
        for pane in ("%999999", "%998877", "%0"):
            self.assertEqual(
                self.server.pane_exists(pane), pane_alive(tmux, pane),
                f"{pane}: server and composer must answer alike")

    def test_a_phantom_pane_is_not_alive_for_the_pilot_either(self) -> None:
        """The concrete phantom the bug was caught on: a missing pane answered by tmux
        with code 0 and an empty line."""
        from composer import pane_alive
        if not shutil_which("tmux"):
            self.skipTest("tmux not installed")
        self.assertFalse(pane_alive(shutil_which("tmux"), "%999999"))
        mine = self._pane_at_startup
        if mine:
            self.assertTrue(pane_alive(shutil_which("tmux"), mine), "a live pane is alive")

    def test_real_tmux_answers_both_ways(self) -> None:
        """The only check here no stub may replace: a real tmux, both answers.

        The rest of this class stubs pane_exists, so a function answering wrongly
        would pass regardless of whether the suite runs inside tmux.

        The second answer matters more than the first: past the session death %N is
        a name with no content, and pane_exists must name exactly it a phantom.
        """
        tmux = shutil_which("tmux")
        if not tmux:
            self.skipTest("tmux not installed")
        # A randomly suffixed name with no foreign kill-session before creating. It used to
        # stand `pairtest-<pid>` with an unconditional `kill-session` BEFORE new-session —
        # two parallel runs sharing one pid, and more often killing a foreign session with the
        # same name. Dropping both.
        session = f"pairtest-{os.getpid()}-{os.urandom(4).hex()}"
        self.addCleanup(subprocess.run, [tmux, "kill-session", "-t", session],
                        capture_output=True, timeout=10)
        made = subprocess.run([tmux, "new-session", "-d", "-s", session],
                              capture_output=True, text=True, timeout=10)
        if made.returncode != 0:
            self.skipTest(f"tmux refuses to create a session: {made.stderr.strip()[:80]}")
        listed = subprocess.run([tmux, "list-panes", "-t", session, "-F", "#{pane_id}"],
                                capture_output=True, text=True, timeout=10)
        panes = listed.stdout.split()
        self.assertTrue(panes, "the session owns a pane")
        pane = panes[0]
        self.assertTrue(self.server.pane_exists(pane), f"a live pane {pane} — alive")

        subprocess.run([tmux, "kill-session", "-t", session], capture_output=True, timeout=10)
        self.assertFalse(self.server.pane_exists(pane),
                         f"{pane} past the session death — a phantom, not a pane")




class TestSingleLiveProcessPerRole(PairTestCase):
    """One live role means one live process (#19.2).

    A second process under the same role name from another pane with a live recorded
    pid is a double, not a restart: registration refuses, the roster untouched.
    """

    def setUp(self) -> None:
        super().setUp()
        import server
        self.server = server

    def live_pid(self, value: bool):
        """Stub the recorded-pid liveness: a foreign test pid is always dead."""
        patcher = mock.patch.object(self.server, "is_pid_alive", return_value=value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def same_pane_as(self, value: bool):
        """Stub the pane comparison: the tmux edge, a mock in unit."""
        patcher = mock.patch.object(self.server, "same_pane", return_value=value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def join_pane(self, pane: str) -> None:
        previous = os.environ.get("TMUX_PANE")
        os.environ["TMUX_PANE"] = pane
        self.addCleanup(lambda: os.environ.__setitem__("TMUX_PANE", previous)
                        if previous is not None else os.environ.pop("TMUX_PANE", None))

    def pane_alive(self, value: bool):
        real = self.server.pane_exists
        self.server.pane_exists = lambda pane: value
        self.addCleanup(setattr, self.server, "pane_exists", real)
        import composer
        real_belongs = composer.pane_belongs_to_project
        composer.pane_belongs_to_project = lambda tmux, pane, root: (True, "")
        self.addCleanup(setattr, composer, "pane_belongs_to_project", real_belongs)

    def register(self, role: str, pane: str, pid: int):
        self.store.mutate(lambda s: s.setdefault("roles", {}).setdefault(role, {}).update(
            {"pane": pane, "pid": pid}))

    def test_second_server_process_on_the_same_role_is_refused(self) -> None:
        """Singleton: two live server.py on one role means the second falls refused.
        The old guard stood on pid+is_pid_alive (flipping on restart, measured: two
        live processes shared the opencode slot). A flock on .server-<role>.lock
        refuses the double at once, with no sorting whose pid is whose.
        """
        first = self.store.acquire_role_lock("agy")
        with self.assertRaises(ProtocolError):
            self.store.acquire_role_lock("agy")
        first.close() if hasattr(first, "close") else None

    def test_role_lock_is_released_when_the_process_dies(self) -> None:
        """A dead lock never sticks: closing the handle releases, and a new process of the
        same role takes the slot with no manual cleanup."""
        first = self.store.acquire_role_lock("agy")
        first.close()
        second = self.store.acquire_role_lock("agy")
        second.close() if hasattr(second, "close") else None

    def test_different_roles_do_not_block_each_other(self) -> None:
        """Slots stay independent: omp and agy hold their locks in parallel."""
        omp = self.store.acquire_role_lock("omp")
        agy = self.store.acquire_role_lock("agy")
        omp.close() if hasattr(omp, "close") else None
        agy.close() if hasattr(agy, "close") else None

    def test_startup_refusal_prints_the_remedy(self) -> None:
        """The lock refusal on startup prints its own remedy (round 92 C4).
        The hint ('kill the double and restart') lived in args[1] while the
        human saw only an actionless traceback. main() must print both."""
        first = self.store.acquire_role_lock("agy")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = main(["--role", "agy", "--state", str(self.root)])
        self.assertEqual(code, 1)
        self.assertIn("already held", err.getvalue())
        self.assertIn("kill the double", err.getvalue())
        first.close() if hasattr(first, "close") else None

    def test_second_live_process_on_another_pane_is_refused(self) -> None:
        """A double: a live pid, a live held pane, another comer — refusal."""
        self.register("agy", "%20", 999999)
        self.live_pid(True)
        self.pane_alive(True)
        self.same_pane_as(False)
        self.join_pane("%21")
        with self.assertRaises(ProtocolError):
            PairSession("agy", self.store).tool_pair_state()
        roles = self.store.read()["roles"]
        self.assertEqual(roles["agy"].get("pid"), 999999)
        self.assertEqual(roles["agy"].get("pane"), "%20")
        kinds = [e["kind"] for e in self.store.read_transcript()]
        self.assertIn("role_collision", kinds)

    def test_restart_on_the_same_pane_updates_pid(self) -> None:
        """A restart: the pid changed, the pane stays — registration passes."""
        self.register("agy", "%20", 999999)
        self.live_pid(False)
        self.pane_alive(True)
        self.same_pane_as(True)
        self.join_pane("%20")
        PairSession("agy", self.store).tool_pair_state()
        self.assertEqual(self.store.read()["roles"]["agy"].get("pid"), os.getpid())

    def test_reused_pid_on_the_same_pane_is_not_a_refusal(self) -> None:
        """A reused pid: the recorded "alive" one, but the pane matches — no refusal.

        os.kill(pid, 0) sees a foreign process as living, so one live-pid fact with no
        second fact (another pane) never refuses.
        """
        self.register("agy", "%20", 999999)
        self.live_pid(True)
        self.pane_alive(True)
        self.same_pane_as(True)
        self.join_pane("%20")
        PairSession("agy", self.store).tool_pair_state()
        self.assertEqual(self.store.read()["roles"]["agy"].get("pid"), os.getpid())

    def test_no_pane_means_no_refusal(self) -> None:
        """An addressless call claims no composer — no refusal."""
        self.register("agy", "%20", 999999)
        self.live_pid(True)
        self.pane_alive(True)
        self.same_pane_as(False)
        # Finding our own pane over the process tree (round 32) finds the launching-shell
        # pane in the suite — the test would stop being addressless.
        import composer
        real_find = composer.find_own_pane
        composer.find_own_pane = lambda tmux: ""
        try:
            PairSession("agy", self.store).tool_pair_state()
        finally:
            composer.find_own_pane = real_find
        kinds = [e["kind"] for e in self.store.read_transcript()]
        self.assertIn("role_collision", kinds)

    def test_dead_held_pane_means_restart_not_duplicate(self) -> None:
        """A held pane dead, the process alive: a restart past the pane death."""
        self.register("agy", "%20", 999999)
        self.live_pid(True)
        self.pane_alive(False)
        self.same_pane_as(False)
        self.join_pane("%21")
        PairSession("agy", self.store).tool_pair_state()
        self.assertEqual(self.store.read()["roles"]["agy"].get("pid"), os.getpid())




class TestPaneIdentityIsNotLiveness(PairTestCase):
    """Pane liveness is not its identity.

    A pane `%N` number outlives the process that held it. Several projects share
    pair-mailbox on this machine, so checking `pane_dead` alone let one project
    drive messages into another agent composer. Two independent facts: the pane
    lives AND belongs to us.
    """

    def test_a_pane_of_another_project_is_refused(self) -> None:
        import composer
        orig = composer.pane_identity
        composer.pane_identity = lambda tmux, pane: {
            "exists": True, "alive": True, "path": "/home/alexios/work/phronesis"}
        try:
            foreign, why = composer.pane_is_foreign(
                "tmux", "%1", Path(__file__).resolve().parents[2])
        finally:
            composer.pane_identity = orig
        self.assertTrue(foreign, "never type into a foreign-project pane")
        self.assertIn("another project", why)

    def test_our_own_pane_is_allowed(self) -> None:
        import composer
        root = Path(__file__).resolve().parents[2]
        orig = composer.pane_identity
        composer.pane_identity = lambda tmux, pane: {
            "exists": True, "alive": True, "path": str(root)}
        try:
            foreign, _ = composer.pane_is_foreign("tmux", "%1", root)
        finally:
            composer.pane_identity = orig
        self.assertFalse(foreign, "our own pane passes")

    def test_an_unprovable_pane_fails_closed(self) -> None:
        """An empty cwd proves nothing, so it refuses, never allows."""
        import composer
        root = Path(__file__).resolve().parents[2]
        orig = composer.pane_identity
        composer.pane_identity = lambda tmux, pane: {
            "exists": True, "alive": True, "path": ""}
        try:
            foreign, why = composer.pane_is_foreign("tmux", "%1", root)
        finally:
            composer.pane_identity = orig
        self.assertTrue(foreign)
        self.assertIn("unproven", why)

    def test_a_dead_pane_is_refused(self) -> None:
        """Mutation showed this case uncovered: a green nothing meant nothing."""
        import composer
        orig = composer.pane_identity
        composer.pane_identity = lambda tmux, pane: {
            "exists": True, "alive": False, "path": "/home/alexios/work/umputum"}
        try:
            foreign, why = composer.pane_is_foreign(
                "tmux", "%1", Path(__file__).resolve().parents[2])
        finally:
            composer.pane_identity = orig
        self.assertTrue(foreign, "never type into a dead pane")
        self.assertIn("dead", why)

    def test_a_missing_pane_is_refused(self) -> None:
        """tmux answers a missing pane with code 0 and an EMPTY output.

        The old inline-probe in _ring took such an answer for "alive" and sent
        send-keys into a phantom number. The same pane must refuse here.
        """
        import composer
        orig = composer.pane_identity
        composer.pane_identity = lambda tmux, pane: {
            "exists": False, "alive": False, "path": ""}
        try:
            foreign, why = composer.pane_is_foreign(
                "tmux", "%999999", Path(__file__).resolve().parents[2])
        finally:
            composer.pane_identity = orig
        self.assertTrue(foreign, "a phantom pane never passes")
        self.assertIn("missing", why)

    def test_a_nonexistent_pane_is_not_reported_as_existing(self) -> None:
        """Live tmux, not a mock: exactly a mock would never catch this bug.

        tmux answers a missing pane with code 0 and an EMPTY output, so exists
        derived from the return code alone lied: pane %49 counted existing. The
        refusal result was safe by accident — alive computed right — but the field
        itself stood untrue, and any future caller would trust it in vain.
        """
        import composer
        tmux = shutil_which("tmux")
        if not tmux:
            self.skipTest("tmux unavailable in this env")
        ident = composer.pane_identity(tmux, "%499999")
        self.assertFalse(ident["exists"], "a missing pane never exists")
        self.assertFalse(ident["alive"])
        self.assertEqual(ident["path"], "")

    def test_a_state_dir_named_pair_resolves_to_the_project_root(self) -> None:
        """A regression on a bug that cost blocking all delivery.

        `Store.root` is the state dir `<project>/.pair`, not the project root.
        Passing it into `pane_is_foreign` refused with "pane belongs to another
        project" for our OWN neighbour pane: `.pair` never parents the pane, it
        lies inside the project. The cross-project injection guard blocked lawful
        in-project delivery too.

        Unit tests on `pane_is_foreign` never saw it: they passed the project root,
        not the value prod passes. Entry-point tests never saw it either: they
        stubbed `pane_is_foreign` itself. The bug walked two lines and surfaced only
        because agy could never reach me live.
        """
        import composer
        state_dir = Path(tempfile.mkdtemp(prefix="pairtest-")) / ".pair"
        state_dir.mkdir(parents=True)
        store = Store(state_dir)
        self.assertEqual(store.root, state_dir)
        self.assertEqual(store.project_root, state_dir.parent,
                         "the project root is the state-dir parent")
        orig = composer.pane_identity
        composer.pane_identity = lambda tmux, pane: {
            "exists": True, "alive": True, "path": str(state_dir.parent)}
        try:
            # the prod value: what the code truly passes
            foreign, why = composer.pane_is_foreign(
                "tmux", "%1", store.project_root)
        finally:
            composer.pane_identity = orig
        self.assertFalse(foreign, f"our own pane must pass, got: {why}")

    def test_a_state_dir_that_is_not_pair_is_its_own_project_root(self) -> None:
        """.pair` is no dir — meaning this IS the root, parent stands extra."""
        state_dir = Path(tempfile.mkdtemp(prefix="pairtest-"))
        store = Store(state_dir)
        self.assertEqual(store.project_root, state_dir)

    def test_a_shared_parent_directory_is_refused(self) -> None:
        """A pane from ~/work shared across projects means not ours.

        Accepting it would return the cross-project injection by the same path.
        """
        import composer
        root = Path("/home/alexios/work/umputum")
        orig = composer.pane_identity
        composer.pane_identity = lambda tmux, pane: {
            "exists": True, "alive": True, "path": "/home/alexios/work"}
        try:
            foreign, why = composer.pane_is_foreign("tmux", "%1", root)
        finally:
            composer.pane_identity = orig
        self.assertTrue(foreign)
        self.assertIn("another project", why)

    def test_ring_falls_back_to_a_pointer_on_another_projects_pane(self) -> None:
        """An unproven pane: typed printing banned, the fallback is a textless
        display-message. No silence, no injection."""
        import composer
        import subprocess as _sp
        self.store.mutate(lambda s: s.setdefault("roles", {})
                          .setdefault("reviewer-agent", {}).update({"pane": "%45"}))
        os.environ.pop("PAIR_DISABLE_RING", None)
        orig_run = _sp.run
        orig_foreign = composer.pane_is_foreign
        called = []
        composer.pane_is_foreign = lambda tmux, pane, root: (
            True, "pane belongs to another project: /home/alexios/work/phronesis")
        _sp.run = lambda *a, **k: called.append(a[0]) or type("R", (), {
            "stdout": "0", "returncode": 0})()
        try:
            result = self.writer._ring("reviewer-agent", "note", "details")
        finally:
            _sp.run = orig_run
            composer.pane_is_foreign = orig_foreign
        self.assertTrue(result["rung"])
        self.assertEqual("display-message", result.get("fallback"))
        self.assertFalse(any("send-keys" in c for c in called),
                         "typed printing into a foreign pane is banned")
        kinds = [e.get("kind") for e in self.store.read_transcript(limit=0)]
        self.assertIn("doorbell_fallback", kinds)

    def test_deliver_falls_back_to_a_pointer_on_another_projects_pane(self) -> None:
        """An unproven pane is no refusal: a textless display-message leaves, the full text
        only in the box. Typed printing banned (injection)."""
        import composer
        import subprocess as _sp
        self.store.mutate(lambda s: (s.setdefault("roles", {}).setdefault(
            "reviewer-agent", {})
            .update({"pid": os.getpid(), "pane": "%55"})))
        orig_foreign = composer.pane_is_foreign
        orig_belongs = composer.pane_belongs_to_project
        orig_run = _sp.run
        called = []
        # The _register sweep drops the pane via pane_belongs_to_project: %55 holds for
        # it, while pane_is_foreign gives the fallback branch in deliver.
        composer.pane_belongs_to_project = lambda tmux, pane, root: (True, "")
        composer.pane_is_foreign = lambda tmux, pane, root: (
            True, "pane belongs to another project: /home/alexios/work/phronesis")
        _sp.run = lambda *a, **k: called.append(a[0]) or type("R", (), {
            "stdout": "", "returncode": 0})()
        try:
            res = self.writer.tool_pair_deliver(to="reviewer-agent", text="payload worth keeping")
        finally:
            composer.pane_is_foreign = orig_foreign
            composer.pane_belongs_to_project = orig_belongs
            _sp.run = orig_run
        # The pointer left by display-message (fallback), the full text only in the box.
        self.assertTrue(res["delivered"])
        self.assertTrue(res["in_pane"])
        self.assertEqual("display-message", res.get("fallback"))
        self.assertTrue(any("display-message" in c for c in called),
                        "fallback goes by display-message, not send-keys")
        self.assertFalse(any("send-keys" in c for c in called),
                         "typed printing into a foreign pane is banned")
        self.assertNotIn("payload worth keeping", str(called),
                         "full text never leaves for a foreign pane")
        read = self.peer.tool_pair_read(wait_seconds=0)
        self.assertEqual(read["count"], 1)
        self.assertIn("payload worth keeping", read["messages"][0]["text"])


class TestOldPythonStillRuns(PairTestCase):
    """The code must import on the human Python.

    Install on macOS fell with 210 tests from one cause: system python3 there is
    3.9, while `str | None`-style annotations compute at import demanding 3.10.
    The test catches it on any machine: it asks not "which version here" but
    "does every module import on the MINIMUM declared version".
    """

    FLOOR = (3, 8)
    ROOT = Path(__file__).resolve().parents[2]

    def modules(self):
        """Supply modules: the server, the composer, and all it lazily imports."""
        pkg = Path(__file__).resolve().parent
        skip = {"test_protocol", "pair_test_base"}
        return sorted(p for p in pkg.glob("*.py")
                      if p.stem not in skip and not p.name.startswith("test_"))

    def test_every_module_imports_on_the_declared_floor(self) -> None:
        if sys.version_info >= self.FLOOR:
            self.skipTest(f"and so on {sys.version_info.major}.{sys.version_info.minor}, "
                          f"nothing to check")
        for path in self.modules():
            with self.subTest(module=path.name):
                proc = subprocess.run(
                    [sys.executable, "-c", f"import {path.stem}"],
                    cwd=str(path.parent), capture_output=True, text=True, timeout=30)
                self.assertEqual(0, proc.returncode,
                                 f"{path.name} never imports on the minimum:\n{proc.stderr}")

    def test_no_module_pep604_annotations_without_the_future_import(self) -> None:
        """`X | None` in annotations is 3.10+. Without `from __future__ import
        annotations` the annotation computes at import dropping the module."""
        for path in self.modules():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            future = any(isinstance(n, ast.ImportFrom) and n.module == "__future__"
                         and any(a.name == "annotations" for a in n.names)
                         for n in tree.body)
            if future:
                continue
            for node in ast.walk(tree):
                ann = None
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    ann = node.returns
                if isinstance(node, ast.arg):
                    ann = node.annotation
                if isinstance(node, ast.AnnAssign):
                    ann = node.annotation
                if ann is None:
                    continue
                if any(isinstance(n, ast.BinOp) and isinstance(n.op, ast.BitOr)
                       and _is_type(n.left) and _is_type(n.right)
                       for n in ast.walk(ann)):
                    self.fail(f"{path.name}:{node.lineno} — an annotation with `|` "
                              f"without `from __future__ import annotations`")


def _is_type(node) -> bool:
    """An `X | Y` annotation side: a name, a subscription, or a None constant."""
    return isinstance(node, (ast.Name, ast.Attribute, ast.Subscript, ast.Constant))
