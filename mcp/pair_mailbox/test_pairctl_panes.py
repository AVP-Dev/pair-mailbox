#!/usr/bin/env python3
"""Tests for binding roles to tmux panes: `pairctl pane` and `pairctl roster`.

Match `pairctl_panes.py`. The three-state `pin_verdict` check moved there too,
and its tests live here for the same reason: the `unknown` state (nothing to
check with) and the `bad` state (proven dead address) differ only in the two
callers’ decisions, and both callers are here.

Run:  python3 mcp/pair_mailbox/test_pairctl_panes.py
"""
from __future__ import annotations

import contextlib
import io
import os
import sys
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pair_test_base import PairTestCase  # noqa: E402
from server import main  # noqa: E402

class TestRosterPrune(PairTestCase):
    """`pairctl roster --prune`.

    The roster is the pair shared memory, and dead-session entries lie in it
    for years: claude, codex and both duo roles still listed on agy’s live
    pane. A dead entry takes space, confuses the report and spoils skip.
    Deleting must be neither silent nor single-fact, but on two independent facts."""

    def ctl(self, *argv: str) -> int:
        return main(["--state", str(self.root), *argv])

    def dead_role(self, role: str, pane: str, pid: int = 999999) -> None:
        self.store.mutate(lambda st: st.setdefault("roles", {}).update(
            {role: {"pane": pane, "pid": pid, "last_seen": "then", "first_seen": "then"}}))

    def test_prune_removes_a_dead_role_whose_pane_is_gone(self) -> None:
        self.dead_role("ghost", "%999999")
        self.assertEqual(0, self.ctl("roster", "--prune"))
        self.assertNotIn("ghost", self.store.read()["roles"])

    def test_prune_keeps_a_dead_role_whose_pane_still_exists(self) -> None:
        """One dead pid never suffices: the agent death may leave a live pane behind,
        and deleting the entry is early.

        The stub goes where the function lives now: `pane_exists` uses
        `pairctl_panes`, and stubbing `server.pane_exists` would affect nothing —
        the test would check the real tmux and pass for the wrong reason. So the
        consumer module is stubbed, not the module where the function is defined.
        """
        self.dead_role("leftover", "%20")
        with mock.patch("pairctl_panes.pane_exists", return_value=True):
            self.assertEqual(0, self.ctl("roster", "--prune"))
        self.assertIn("leftover", self.store.read()["roles"])

    def test_prune_removes_a_dead_role_whose_pane_belongs_to_someone_living(self) -> None:
        """The third case, the one combat waited for: the pane is alive but belongs
        to another live role. The entry is not just stale — it points away."""
        self.dead_role("claude", "%20")
        self.store.mutate(lambda st: st.setdefault("roles", {}).update(
            {"agy": {"pane": "%20", "pid": os.getpid()}}))
        with mock.patch("pairctl_panes.pane_exists", return_value=True):
            self.assertEqual(0, self.ctl("roster", "--prune"))
        roles = self.store.read()["roles"]
        self.assertNotIn("claude", roles)
        self.assertIn("agy", roles, "never touch a live role")

    def test_prune_never_touches_a_living_role(self) -> None:
        self.store.mutate(lambda st: st.setdefault("roles", {}).update(
            {"agy": {"pane": "%20", "pid": os.getpid()}}))
        self.assertEqual(0, self.ctl("roster", "--prune"))
        self.assertIn("agy", self.store.read()["roles"])

    def test_prune_refuses_to_remove_the_writer(self) -> None:
        """Otherwise the roster could be wiped together with the right holder,
        without a word."""
        self.writer.tool_pair_claim(reason="writer")
        self.dead_role("writer-agent", "%999999")
        self.assertEqual(2, self.ctl("roster", "--prune"))
        self.assertIn("writer-agent", self.store.read()["roles"])

    def test_roster_without_prune_only_looks(self) -> None:
        self.dead_role("ghost", "%999999")
        self.assertEqual(0, self.ctl("roster"))
        self.assertIn("ghost", self.store.read()["roles"])

    def test_pruning_is_in_the_audit_trail(self) -> None:
        self.dead_role("ghost", "%999999")
        self.ctl("roster", "--prune")
        self.assertIn("roster_pruned", [e["kind"] for e in self.store.read_transcript()])



class TestPaneOverride(PairTestCase):
    """A human-hand pane-binding override (`pairctl pane`).

    The cause is measured, not guessed: `_register` takes the address from the
    process `TMUX_PANE` environment and rewrites `pane` on every call of any
    tool. So hand-editing `state.json` fixed nothing — the "fix" lived exactly
    till the next ring. The `pane_pin` key reads before the environment and so
    survives re-registration.

    Such a key costs a hole risk in the cross-project isolation, so the pinned
    address must pass the same check as the automatic one: the pane is alive
    AND the pane is ours.
    """

    def setUp(self) -> None:
        super().setUp()
        import server
        self.server = server

    def ctl(self, *argv: str) -> int:
        return main(["--state", str(self.root), *argv])

    def pinned(self) -> dict:
        return (self.store.read().get("roles") or {}).get("opencode", {})

    def with_pane(self, pane: str | None) -> None:
        """The process environment: either the suite’s live pane or its absence.

        Restored is the pane the suite started from, not what lay in the variable
        at call time: the base `setUp` already scrubbed the variable, and a
        cleanup returning "the old" would wipe what `tearDown` already restored.
        unittest order is `tearDown`, then cleanup — and over it every following
        suite test started skipping silently while the suite stayed green.
        Proven: five tests turned into skips, and none failed.
        """
        before = self._pane_at_startup
        if pane is None:
            os.environ.pop("TMUX_PANE", None)
        else:
            os.environ["TMUX_PANE"] = pane

        def restore() -> None:
            if before is None:
                os.environ.pop("TMUX_PANE", None)
            else:
                os.environ["TMUX_PANE"] = before

        self.addCleanup(restore)

    def live_pane(self) -> str:
        pane = self._pane_at_startup
        if not pane:
            self.skipTest("suite runs outside tmux")
        return pane

    def as_ours(self):
        """Count the suite pane as ours, with no check-result stubbing.

        The suite runs on state in a temp dir, but the pane lives in the real
        project, so the pane authenticity here is always "foreign". Stubbing it
        to "ours" must be explicit: otherwise the test would check only refusal,
        not acceptance.

        Both questions are stubbed. `_register` asks "this project’s pane?",
        while `pair_deliver` asks "not foreign?". With the second check added the
        stub stayed on the first, and the acceptance check became a refusal check:
        the test demanded the pane recorded but got it dropped.
        """
        return mock.patch.multiple(
            "composer",
            pane_is_foreign=mock.Mock(return_value=(False, "")),
            pane_belongs_to_project=mock.Mock(return_value=(True, "")),
        )

    def bind(self, role: str, pane: str, pid: int) -> None:
        self.store.mutate(lambda st: st.setdefault("roles", {}).update(
            {role: {"pane": pane, "pid": pid, "first_seen": "then",
                    "last_seen": "then", "reads": 0, "sends": 0}}))

    def register(self, role: str) -> None:
        """Drive the role registration — what the server does on every call.

        Through `mutate`, not on a read: `_register` edits state in place, and a
        suite feeding it a copy would check the copy, not what the server would
        save next time.
        """
        session = self.server.PairSession(role, self.store)
        self.store.mutate(session._register)

    def test_the_override_survives_a_reregistration(self) -> None:
        """The main property everything was built for.

        Without it the override is a hand-written state.json entry that goes stale
        in one call — exactly the breakage the human fixes.
        """
        pane = self.live_pane()
        self.with_pane(pane)
        self.bind("opencode", pane, 999999)
        with self.as_ours():
            self.assertEqual(0, self.ctl("pane", "--role", "opencode", pane))

        # The recorded pane leaves while no pane sits in the environment. That is what
        # a re-registration of a server launched from the wrong pane looks like.
        #
        # The diversion is explicit: `pairctl pane` itself writes `pane` at setup,
        # so without it the test would pass even with the override fully disabled —
        # nothing to check.
        self.store.mutate(lambda st: st["roles"]["opencode"].update(
            {"pane": "%12345"}))
        self.with_pane(None)
        with self.as_ours():
            self.register("opencode")
        self.assertEqual(pane, self.pinned().get("pane"),
                         "the override must survive TMUX_PANE loss")
        self.assertEqual(pane, self.pinned().get("pane_pin"),
                         "the pin must stay, not fire once")

    def test_a_stale_override_is_dropped_and_says_why(self) -> None:
        """A stale-pane pin cannot hang forever.

        Otherwise delivery would leave into nowhere silently, and the human would
        believe the role is bound. Dropping must surface in the transcript: a
        silently dropped override reads as one that never was.
        """
        self.with_pane(self.live_pane())
        self.bind("opencode", self._pane_at_startup, 999999)
        # A nonexistent-pane address is refused at entry, not "just in case".
        #
        # Checked as exactly "no pane", not just "refused": a pane that is gone
        # is also refused by the foreign check, because tmux reports no cwd for it.
        # A bare "refused" assertion would pass even with liveness gutted — proven by mutation.
        # the liveness check — proven by mutation.
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(1, self.ctl("pane", "--role", "opencode", "%987654"))
        self.assertIn("no pane left", stderr.getvalue())
        self.assertNotIn("pane_pin", self.pinned())

        # Now plant a stale pin directly — how it looks after the human closed the
        # window while the state stayed.
        self.store.mutate(lambda st: st["roles"]["opencode"].update(
            {"pane_pin": "%987654", "pane_pin_why": "pinned by human"}))
        self.register("opencode")
        self.assertNotIn("pane_pin", self.pinned(),
                         "a dead-pane address must be dropped")
        kinds = [row["kind"] for row in self.store.read_transcript()]
        self.assertIn("pane_pin_dropped", kinds)

    def test_a_foreign_pane_is_refused(self) -> None:
        """The override must never open a hole in the cross-project isolation.

        The address is checked exactly for that. Without the check a human could
        route the role mail into another project with one input — and break what
        items 1 and 2 fixed.
        """
        self.with_pane(self.live_pane())
        self.bind("opencode", self._pane_at_startup, 999999)
        with mock.patch("composer.pane_is_foreign",
                        return_value=(True, "another project: /somewhere/else")):
            self.assertEqual(1, self.ctl("pane", "--role", "opencode", "%1"))
        self.assertNotIn("pane_pin", self.pinned())

    def test_without_an_override_the_pane_still_comes_from_tmux(self) -> None:
        """The override must stay narrow: without it the old behaviour holds.

        Otherwise this is not an "override" but a new rule, and an agent that
        already works suddenly sits on the wrong pane.
        """
        pane = self.live_pane()
        self.with_pane(pane)
        self.bind("opencode", "old", 999999)
        with self.as_ours():
            self.register("opencode")
        from composer import _tmux, session_address
        expected = session_address(_tmux(), pane) or pane
        self.assertEqual(expected, self.pinned().get("pane"))

    def test_clearing_returns_the_pane_to_tmux(self) -> None:
        """Dropping the override must return control to the environment.

        Otherwise the only way to return the role to its pane is editing
        state.json — the original breakage.
        """
        pane = self.live_pane()
        self.with_pane(pane)
        self.bind("opencode", pane, 999999)
        with self.as_ours():
            self.assertEqual(0, self.ctl("pane", "--role", "opencode", pane))
        self.assertEqual(0, self.ctl("pane", "--role", "opencode", "--clear"))
        self.assertNotIn("pane_pin", self.pinned())
        with self.as_ours():
            self.register("opencode")
        from composer import _tmux, session_address
        self.assertEqual(session_address(_tmux(), pane) or pane,
                         self.pinned().get("pane"))

    def test_the_override_is_not_logged_on_every_call(self) -> None:
        """The journal must not fill with identical lines.

        Registration runs on every call of any tool. Writing on every call would
        eat exactly the transcript ceiling this round fixes — the override would
        become a new noise source.
        """
        pane = self.live_pane()
        self.with_pane(None)
        self.bind("opencode", pane, 999999)
        with self.as_ours():
            self.assertEqual(0, self.ctl("pane", "--role", "opencode", pane))
        for _ in range(3):
            self.register("opencode")
        kinds = [row["kind"] for row in self.store.read_transcript()]
        self.assertLessEqual(
            kinds.count("pane_pin_honoured"), 1,
            "a record belongs on change, not on every call")


    def test_positional_role_and_address_bind_without_flag(self) -> None:
        """The positional shape from the card: `pane <role> <addr>` with no --role.

        Otherwise after a window restart the binding fixes only through the long
        shape while the card demands exactly `pane <role> <tmux-pane>`. The
        environment is scrubbed on purpose: otherwise the $TMUX_PANE fallback
        would mask a broken positional parse (proven by mutation).
        """
        pane = self.live_pane()
        self.with_pane(None)
        self.bind("opencode", pane, 999999)
        with self.as_ours():
            self.assertEqual(0, self.ctl("pane", "opencode", pane))
        self.assertEqual(pane, self.pinned().get("pane_pin"))

    def test_tmux_pane_env_is_used_only_with_current_flag(self) -> None:
        """$TMUX_PANE reads only with an explicit --current (the %2205 regression).

        Without the flag `pane <role>` from a live pane only shows: otherwise two
        polls in a row (`pane opencode` + `pane pi` from one window) quietly seat
        both roles on one pane, and the supervisor skips them as indistinguishable.
        Measured on live state 2026-10-06.
        """
        pane = self.live_pane()
        self.with_pane(pane)
        self.bind("opencode", pane, 999999)
        with self.as_ours():
            self.assertEqual(0, self.ctl("pane", "opencode"))
        self.assertNotIn("pane_pin", self.pinned(),
                         "showing from a live pane must write nothing")
        with self.as_ours():
            self.assertEqual(0, self.ctl("pane", "--role", "opencode", pane))
        with self.as_ours():
            self.assertEqual(0, self.ctl("pane", "opencode"))
        self.assertEqual(pane, self.pinned().get("pane_pin"))
        self.store.mutate(lambda st: (st["roles"]["opencode"].pop("pane_pin", None),
                                      st["roles"]["opencode"].pop("pane_pin_why", None)))
        with self.as_ours():
            self.assertEqual(0, self.ctl("pane", "opencode", "--current"))
        self.assertEqual(pane, self.pinned().get("pane_pin"))

    def test_show_when_no_address_and_no_tmux_pane(self) -> None:
        """No address and no $TMUX_PANE — a show, not an invented address."""
        pane = self.live_pane()
        self.with_pane(None)
        self.bind("opencode", pane, 999999)
        with self.as_ours():
            self.assertEqual(0, self.ctl("pane", "--role", "opencode", pane))
        self.with_pane(None)
        self.assertEqual(0, self.ctl("pane", "--role", "opencode"))
        self.assertEqual(pane, self.pinned().get("pane_pin"))

class TestPanes(PairTestCase):
    """`pairctl panes`: live tmux panes -> project -> role -> liveness.

    Read-only: never touches state, never writes the journal. Answers "which pane
    is whose", once needing ps + tmux + state.json (task 53: panes climbing at
    each other across projects).
    """

    def ctl_panes(self) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["--state", str(self.root), "panes"])
        return code, out.getvalue()

    def fake_tmux(self, rows: str):
        proc = mock.Mock()
        proc.returncode = 0
        proc.stdout = rows
        proc.stderr = ""
        return mock.patch("pairctl_panes.shutil.which", return_value="/usr/bin/tmux"), \
            mock.patch("subprocess.run", return_value=proc)

    def test_shows_our_and_foreign_panes(self) -> None:
        """Our pane is marked OURS, the foreign one — foreign."""
        self.store.mutate(lambda st: st.setdefault("roles", {}).update(
            {"writer-agent": {"pane": "proj:0.0", "pid": os.getpid(),
                              "first_seen": "then", "last_seen": "then"}}))
        rows = ("proj:0.0\tproj:0.0\t%s\tbash\t%d\t\n"
                "other:0.0\tother:0.0\t/tmp\tbash\t%d\t\n") % (
                    self.root, os.getpid(), os.getpid())
        w, r = self.fake_tmux(rows)
        with w, r:
            code, out = self.ctl_panes()
        self.assertEqual(0, code)
        self.assertIn("OURS", out)
        self.assertIn("foreign", out)
        self.assertIn("writer-agent", out)

    def test_without_tmux_reports_instead_of_lying(self) -> None:
        with mock.patch("pairctl_panes.shutil.which", return_value=None):
            self.assertEqual(1, self.ctl_panes()[0])

    def test_does_not_write_state_or_transcript(self) -> None:
        """Reading must leave no traces."""
        before = self.store.read()
        n_before = len(self.store.read_transcript())
        rows = "p:0.0\tp:0.0\t/tmp\tbash\t%d\t\n" % os.getpid()
        w, r = self.fake_tmux(rows)
        with w, r:
            self.ctl_panes()
        self.assertEqual(before["roles"], self.store.read()["roles"])
        self.assertEqual(n_before, len(self.store.read_transcript()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
